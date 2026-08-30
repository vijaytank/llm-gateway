"""
gateway/router_hook.py — LiteLLM custom router callback that reads model scores
and circuit-breaker state from Redis to influence model selection.

Per Issue 9 fix: The routing brain runs as a separate process (same container,
supervisord). LiteLLM's CustomLogger writes request events to Redis stream.
This callback reads from Redis on each routing decision.

Design (per master plan Issue 5 and Issue 9):
- Models with circuit = open → excluded from routing
- Models with circuit = half_open → score = 0 (last resort)
- Models with a score in Redis → use it as sort key within fallback chain
- Falls back to static priority order if Redis is unreachable (fail-safe)
"""

import os
import json
from typing import Dict, Any, List, Optional, Tuple

import redis

from brain.circuit_breaker import CircuitBreakerManager
from brain.provider_circuit import ProviderCircuitManager
from brain.connectivity_monitor import ConnectivityMonitor, OFFLINE_KEY
from brain.scorer import compute_score
from schemas.config import GatewayConfig, RoutingDefaults

# Providers whose models remain routable while the gateway is in offline mode.
LOCAL_PROVIDERS = frozenset({"local", "ollama", "vllm"})
LOCAL_MODEL_PREFIXES = ("local-",)


def is_local_model(model_name: str) -> bool:
    """A model is 'local' if it lives in the local pool (prefix or registry provider)."""
    lowered = (model_name or "").lower()
    return any(lowered.startswith(p) for p in LOCAL_MODEL_PREFIXES)


class RouterHook:
    """
    LiteLLM CustomRouter callback that consults Redis for live model scores
    and circuit breaker state.
    
    Called by LiteLLM on each routing decision. Returns an influence value that
    affects model selection within the fallback chain.
    """
    
    def __init__(self, redis_client: Optional[redis.Redis] = None,
                 gateway_config: Optional[GatewayConfig] = None):
        self.redis = redis_client or redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        self.gateway_config = gateway_config or self._load_config()
        self.cb_manager = CircuitBreakerManager(self.redis)
        self.provider_circuit = ProviderCircuitManager(self.redis)
        self.defaults = self.gateway_config.routing_defaults
        # Phase 3 AC: local.enabled=false must prevent ANY local model from
        # being used, even during total cloud outages (plan test_local_disabled).
        try:
            self.local_enabled = bool(
                getattr(self.gateway_config.providers, "local", None)
                and self.gateway_config.providers.local.enabled
            )
        except Exception:
            # Fail-open on config problems: treat local as disabled only if we
            # can positively confirm it's disabled; otherwise assume enabled.
            self.local_enabled = True
    
    def _load_config(self) -> GatewayConfig:
        """Load GatewayConfig from environment or file."""
        try:
            import yaml
            config_path = os.environ.get("GATEWAY_CONFIG_PATH", "gateway_config.yaml")
            with open(config_path) as f:
                data = yaml.safe_load(f)
            from schemas.config import GatewayConfig
            return GatewayConfig.model_validate(data)
        except Exception:
            # Return defaults if config can't be loaded
            from schemas.config import RoutingDefaults
            return GatewayConfig(routing_defaults=RoutingDefaults())
    
    def is_offline(self) -> bool:
        """Check the offline flag in Redis (fail-open: Redis down → not offline)."""
        try:
            return bool(self.redis.get(OFFLINE_KEY))
        except Exception:
            return False

    def offline_route_decision(self, fallback_chain: List[str]) -> Tuple[Optional[str], str]:
        """
        Decide routing while offline mode is active.

        Returns (model_name_or_None, reason). Only local-pool models remain
        routable; if the chain has none — or local is disabled in config —
        return a reason so callers respond 503:
        - "offline_no_local_models" when local is enabled but unavailable
        - "all_free_models_exhausted" when local.enabled=false (plan AC)
        """
        if not self.local_enabled:
            return None, "all_free_models_exhausted"
        local_candidates = [m for m in fallback_chain if is_local_model(m)]
        # Prefer healthy, un-circuited local models by score.
        ranked: List[Tuple[float, str]] = []
        for model in local_candidates:
            state = self.cb_manager.get_state(model)
            if state == "open":
                continue
            try:
                score = float(self.redis.get(f"gateway:model:{model}:score") or 0.5)
            except Exception:
                score = 0.5
            ranked.append((score if state != "half_open" else 0.0, model))
        if not ranked:
            return None, "offline_no_local_models"
        ranked.sort(reverse=True)
        return ranked[0][1], "offline_mode_local_only"

    def influence_model_selection(self, model_name: str, fallback_chain: List[str],
                                  provider: Optional[str] = None,
                                  latency_ms: Optional[int] = None,
                                  input_tokens: Optional[int] = None,
                                  output_tokens: Optional[int] = None) -> Tuple[int, str]:
        """
        Compute the routing influence for a model.
        
        Returns (influence_score, reason) where:
        - influence_score: -1 (excluded), 0 (last resort / half_open), or positive score
        - reason: Human-readable explanation
        
        The influence is used by LiteLLM's router to prioritize/exclude models
        within the fallback chain.
        """
        try:
            # 0. Offline mode: cloud models are excluded entirely. Local
            # disabled in config → local models excluded too (plan AC:
            # test_local_disabled — no local model used even during outages).
            if self.is_offline():
                if not is_local_model(model_name):
                    return -1, "offline_mode_cloud_excluded"
                if not self.local_enabled:
                    return -1, "local_disabled"

            # 1. Check circuit breaker state
            circuit_state = self.cb_manager.get_state(model_name)
            
            if circuit_state == "open":
                # Circuit is open — exclude this model
                return -1, "circuit_open"
            
            if circuit_state == "half_open":
                # Phase 5 refinement: half-open probes are throttled to 1 per
                # HALF_OPEN_PROBE_INTERVAL_S (thundering-herd protection). The
                # first caller acquires the probe slot; everyone else routes
                # as last-resort fallback without touching the model.
                if self.cb_manager.try_acquire_half_open_probe(model_name):
                    return 0, "circuit_half_open_probe"
                return 0, "circuit_half_open_throttled"
            
            # 2. Check if we have a score in Redis
            score_key = f"gateway:model:{model_name}:score"
            redis_score = None
            if self.redis:
                raw_score = self.redis.get(score_key)
                if raw_score is not None:
                    try:
                        redis_score = float(raw_score)
                    except (ValueError, TypeError):
                        redis_score = None
            
            # 3. If no Redis score, compute using the scoring formula
            # (only if Redis is unavailable — fail-safe mode)
            if redis_score is None:
                # Compute score using the formula from Issue 5
                try:
                    computed_score = compute_score(
                        model_name=model_name,
                        provider=provider,
                        status="success",  # Default; in production would use actual history
                        latency_ms=latency_ms,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        window_size=self.defaults.moving_avg_window,
                    )
                    redis_score = computed_score
                    # Store in Redis for future use
                    if self.redis:
                        ttl = self.defaults.health_check_base_interval_seconds
                        self.redis.setex(score_key, ttl, str(computed_score))
                except Exception:
                    # If computation fails, use default
                    redis_score = 0.5
            
            # 4. Return the influence score with reason
            # Phase 5: a provider flagged low-priority (3+ model circuits
            # opened within 5 min) gets its models demoted — still routable
            # (never excluded) but ranked below healthy providers' models.
            if redis_score >= 0.8:
                base = int(redis_score * 10)
            elif redis_score >= 0:
                base = int(redis_score * 10)
            else:
                return -1, "circuit_excluded_or_negative"

            if provider and self.provider_circuit.get_priority(provider) == "low":
                # Demote but keep routable: cap influence below average.
                return 1, "provider_low_priority"

            return base, "high_score" if base >= 6 else (
                "good_score" if base >= 3 else "average_score")
            
        except Exception as e:
            # Fail-safe: if anything goes wrong (e.g. Redis unreachable), keep
            # the model routable at last-resort priority so static fallback
            # chain order still applies — never hard-exclude on infra errors.
            return 0, f"error:{str(e)[:50]}"
    
    def should_exclude_model(self, model_name: str, provider: Optional[str] = None,
                             latency_ms: Optional[int] = None,
                             input_tokens: Optional[int] = None,
                             output_tokens: Optional[int] = None) -> bool:
        """
        Check if a model should be excluded from routing based on circuit breaker
        and score state.
        
        Returns True if the model should be excluded.
        """
        # Check circuit breaker
        circuit_state = self.cb_manager.get_state(model_name)
        if circuit_state == "open":
            return True
        
        if circuit_state == "half_open":
            # Half-open: exclude except for one probe request
            # In production, allow exactly one request through
            return True
        
        # Check score — if score is very low, consider excluding
        score_key = f"gateway:model:{model_name}:score"
        if self.redis:
            try:
                score = float(self.redis.get(score_key) or -1)
                if score < 0.1:  # Very low score → exclude
                    return True
            except (ValueError, TypeError):
                pass
        
        return False
    
    def get_fallback_priority(self, model_name: str, fallback_chain: List[str]) -> List[str]:
        """
        Reorder the fallback chain based on Redis scores and circuit breaker states.
        
        Models with high scores and open circuits move to the front.
        Models with open circuits move to the back.
        Models with half-open state go after closed models but before excluded.

        Offline mode: cloud models sink to the end (excluded), local models
        surface to the front — so a fully-offline chain routes local-only, and
        an offline chain with no locals yields only excluded entries (callers
        respond 503 {"error": "offline_no_local_models"}).
        """
        offline = self.is_offline()

        def sort_key(model: str) -> Tuple[int, float]:
            if offline:
                if not is_local_model(model):
                    return (-1, 0.0)  # cloud excluded while offline
                if not self.local_enabled:
                    return (-1, 0.0)  # local disabled → also excluded
                try:
                    s = float(self.redis.get(f"gateway:model:{model}:score") or 0.5)
                except Exception:
                    s = 0.5
                return (1, s)
            return (0, 0.0)

        scored_chain = []
        
        for model in fallback_chain:
            influence, reason = self.influence_model_selection(
                model, fallback_chain,
                provider=None, latency_ms=None,
                input_tokens=None, output_tokens=None
            )
            
            if influence >= 6:  # Score >= 0.6
                # High priority — move to front (but after already-placed high-scorers)
                scored_chain.insert(0, model)
            elif influence >= 0:
                # Middle or low priority — appended in order
                scored_chain.append(model)
            else:  # -1 — excluded: dropped entirely (review F-M14; previously
                # appended to the end, keeping them routable)
                continue
        
        # Remove duplicates while preserving order
        seen = set()
        unique_chain = []
        for model in scored_chain:
            if model not in seen:
                seen.add(model)
                unique_chain.append(model)
        
        # NOTE (review F-M14): models dropped as excluded (-1 influence) stay
        # dropped — the old "ensure all present" backfill re-added them,
        # silently defeating exclusion.

        # Offline mode: stable-sort so local models lead and cloud models sink
        # (stable sort preserves score-based order within each group).
        if offline:
            unique_chain.sort(key=sort_key, reverse=True)

        return unique_chain

    def resolve_fallback_for_model(self, model_name: str) -> Optional[str]:
        """
        Resolve the appropriate capability fallback group when a specific model
        is circuited, unavailable, or excluded from routing.
        """
        if not model_name or model_name.startswith("auto-"):
            return model_name

        lowered = model_name.lower()
        if "reasoning" in lowered or "r1" in lowered or "70b" in lowered or "qwq" in lowered:
            candidate = "auto-reasoning-free"
        elif "code" in lowered or "coder" in lowered:
            candidate = "auto-code-free"
        else:
            candidate = "auto-free"

        # If candidate group itself is not completely open, return it
        if self.cb_manager.get_state(candidate) != "open":
            return candidate
        return "auto-free"