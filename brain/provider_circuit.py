"""
brain/provider_circuit.py — Provider-level circuit breaker (Phase 5)

Per master plan Phase 5 deliverable 1: if 3+ models from the same provider
open their individual circuits within 5 minutes, mark all REMAINING models
from that provider as priority=low for 30 minutes — WITHOUT a full circuit
trip. This avoids false positives from partial provider outages: requests can
still reach the provider as last resort, but healthy other-provider models
are preferred first.

Redis keys:
    gateway:provider:{provider}:circuit_open_events  — sorted set of model
        names scored by open-timestamp (window = ZREMRANGEBYSCORE)
    gateway:provider:{provider}:priority             — "low" with TTL when
        the provider is deprioritized

The router hook consults get_priority() and sorts flagged providers' models
after everything else; it never hard-excludes them.
"""

import time
from typing import Optional

from brain.config import (
    PROVIDER_CIRCUIT_MODEL_THRESHOLD,
    PROVIDER_CIRCUIT_WINDOW_SECONDS,
    PROVIDER_CIRCUIT_LOW_PRIORITY_S,
)


class ProviderCircuitManager:
    """Tracks per-provider circuit-open events and deprioritizes flapping providers."""

    def __init__(self, redis_client=None):
        self.redis = redis_client

    def _events_key(self, provider: str) -> str:
        return f"gateway:provider:{provider}:circuit_open_events"

    def _priority_key(self, provider: str) -> str:
        return f"gateway:provider:{provider}:priority"

    def record_model_circuit_open(self, provider: str, model_name: str) -> None:
        """Record that a model of this provider just opened its circuit.

        Called by the brain's stream reader whenever a circuit transitions to
        open. If enough distinct models opened within the window, flag the
        whole provider as low-priority.
        """
        if not self.redis or not provider:
            return

        now = time.time()
        events_key = self._events_key(provider)

        # Drop events outside the window, add this one, prune to distinct models.
        cutoff = now - PROVIDER_CIRCUIT_WINDOW_SECONDS
        self.redis.zremrangebyscore(events_key, "-inf", cutoff)
        # Same model re-opening refreshes its timestamp rather than double-counting.
        self.redis.zrem(events_key, model_name)
        self.redis.zadd(events_key, {model_name: now})
        self.redis.expire(
            events_key, PROVIDER_CIRCUIT_WINDOW_SECONDS + PROVIDER_CIRCUIT_LOW_PRIORITY_S
        )

        distinct_models = self.redis.zcard(events_key)
        if distinct_models >= PROVIDER_CIRCUIT_MODEL_THRESHOLD:
            # Deprioritize WITHOUT opening circuits on remaining models.
            self.redis.setex(
                self._priority_key(provider),
                PROVIDER_CIRCUIT_LOW_PRIORITY_S,
                "low",
            )

    def get_priority(self, provider: str) -> str:
        """Return "low" while the provider is deprioritized, else "normal".

        Fail-safe: any Redis error → "normal" (never block routing on infra).
        """
        if not self.redis or not provider:
            return "normal"
        try:
            value = self.redis.get(self._priority_key(provider))
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return value or "normal"
        except Exception:
            return "normal"

    def clear_provider_flag(self, provider: str) -> None:
        """Manually reset a provider's low-priority flag (runbook procedure)."""
        if not self.redis:
            return
        try:
            self.redis.delete(self._priority_key(provider))
            self.redis.delete(self._events_key(provider))
        except Exception:
            pass
