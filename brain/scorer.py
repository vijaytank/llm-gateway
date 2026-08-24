"""
brain/scorer.py — Model score computation using the formula from Issue 5

Maintains a rolling window (last N requests) per model in Redis using a sorted set
or list. Computes score using the formula defined in Issue 5 fix.

Score formula:
    score(model) = (success_rate * 0.40)
                 + ((1 - normalized_latency) * 0.35)
                 + (quota_headroom_pct * 0.25)

where:
  normalized_latency = min(latency_ms, LATENCY_CRITICAL_THRESHOLD_MS) / LATENCY_CRITICAL_THRESHOLD_MS
  quota_headroom_pct = 1.0 - (used / limit)   [0.0 to 1.0]
  success_rate       = successes / (successes + failures)  [last N requests]

Models with an open circuit breaker get score = -1 (excluded).
Models in half-open state get score = 0 (last resort within tier).
"""

import json
import time
from typing import Dict, Any, Optional, Tuple

from brain.config import (
    LATENCY_SLOW_THRESHOLD_MS,
    LATENCY_CRITICAL_THRESHOLD_MS,
    SCORE_WEIGHT_SUCCESS_RATE,
    SCORE_WEIGHT_LATENCY,
    SCORE_WEIGHT_QUOTA_HEADROOM,
    SCORE_WEIGHTS,
    QUOTA_DEPRIORITIZE_THRESHOLD,
    MOVING_AVG_WINDOW,
)


# In-memory store for per-model rolling windows (key: "model:provider", value: list of scores)
# In production, this would be in Redis, but for Phase 2 we use Redis sorted sets.
_redis_client = None

def set_redis_client(client):
    """Install the process-wide Redis client used for circuit checks/stats."""
    global _redis_client
    _redis_client = client


def get_redis_client():
    return _redis_client


def get_redis():
    """Resolve a Redis client: injected one, else lazily from env."""
    if _redis_client is not None:
        return _redis_client
    import os
    import redis as redis_lib
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis_lib.from_url(
            url, decode_responses=True,
            socket_connect_timeout=0.2, socket_timeout=0.5)
        client.ping()
        return client
    except Exception:
        return None


_MODEL_WINDOW_KEY = "gateway:model:{model}:latency_window"
_MODEL_OUTCOME_KEY = "gateway:model:{model}:outcome_window"
_MODEL_QUOTA_RPM_KEY = "gateway:model:{model}:quota:rpm"
_MODEL_QUOTA_RPD_KEY = "gateway:model:{model}:quota:rpd"


def compute_score(
    model_name: str,
    provider: Optional[str] = None,
    status: str = "success",
    latency_ms: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    window_size: int = MOVING_AVG_WINDOW,
) -> float:
    """
    Compute the routing score for a model.
    
    Args:
        model_name: Name of the model
        provider: Optional provider name
        status: Request status ("success", "error", "fallback")
        latency_ms: Request latency in milliseconds
        input_tokens: Input token count
        output_tokens: Output token count
        window_size: Size of the rolling window
    
    Returns:
        Score value. Higher is better. Returns -1 if circuit breaker is open.
        Returns 0 if circuit breaker is half-open.
    """
    # Check circuit breaker state first (uses the module-level Redis client)
    from brain.circuit_breaker import CircuitBreakerManager
    cb = CircuitBreakerManager(get_redis())
    
    circuit_state = cb.get_state(model_name)
    if circuit_state == "open":
        return -1.0  # Excluded from routing
    elif circuit_state == "half_open":
        return 0.0  # Last resort within tier
    
    # Retrieve rolling window data from Redis
    # Get success rate from the rolling window
    success_rate = _get_success_rate(redis_client=get_redis(), model_name=model_name, window_size=window_size)
    
    # Get normalized latency from the rolling latency window
    normalized_latency = _get_normalized_latency(
        redis_client=get_redis(), model_name=model_name, latency_ms=latency_ms,
        critical_threshold=LATENCY_CRITICAL_THRESHOLD_MS
    )
    
    # Get quota headroom from the live RPM/RPD counters
    quota_headroom = _get_quota_headroom(redis_client=get_redis(), model_name=model_name)
    
    # Compute the weighted score
    score = (
        success_rate * SCORE_WEIGHT_SUCCESS_RATE
        + (1.0 - normalized_latency) * SCORE_WEIGHT_LATENCY
        + quota_headroom * SCORE_WEIGHT_QUOTA_HEADROOM
    )
    
    # Clamp to valid range [0, 1] with -1 for open circuit
    return max(-1.0, min(1.0, score))


def _get_success_rate(redis_client, model_name: str, window_size: int) -> float:
    """Success rate over the ROLLING last-N outcome window.

    The stream reader maintains gateway:model:{m}:outcome_window — a capped
    list of "1" (success) / "0" (failure) entries. This replaces the old
    unbounded cumulative hash so fresh failures are not masked by history.
    """
    try:
        if redis_client is None:
            return 0.5
        raw = redis_client.lrange(_MODEL_OUTCOME_KEY.format(model=model_name),
                                  -window_size, -1)
        outcomes = [1 if str(v).strip() == "1" else 0 for v in raw]
        if not outcomes:
            return 0.5  # Default: neutral score when no data
        return sum(outcomes) / len(outcomes)
    except Exception:
        return 0.5  # Default neutral score on error


def _get_normalized_latency(
    redis_client, model_name: str, latency_ms: Optional[int],
    critical_threshold: int = LATENCY_CRITICAL_THRESHOLD_MS
) -> float:
    """Get the normalized latency from the rolling window.

    Per the Issue-5 formula the latency term is the model's rolling average
    over the last N requests (Redis list gateway:model:{m}:latency_window,
    maintained by the stream reader) — not just the single latest request.
    The current request's latency is included as the newest sample.
    """
    try:
        samples: list[float] = []
        if redis_client is not None:
            try:
                raw = redis_client.lrange(_MODEL_WINDOW_KEY.format(model=model_name),
                                          -MOVING_AVG_WINDOW, -1)
                samples = [float(v) for v in raw if str(v).replace(".", "", 1).isdigit()]
            except Exception:
                samples = []
        if latency_ms is not None:
            samples.append(float(latency_ms))

        if not samples:
            # No latency data at all: neutral (neither fast nor slow)
            if critical_threshold > 0:
                return 0.5
            return 1.0

        avg_latency = sum(samples) / len(samples)
        # Normalize: min(avg_latency, critical_threshold) / critical_threshold
        normalized = min(avg_latency, critical_threshold) / critical_threshold
        return round(normalized, 4)

    except Exception:
        return 0.5  # Default neutral on error


# Conservative default limits when the registry carries no quota data for a
# model (mirrors seed_model_registry.py values).
_DEFAULT_RPM_LIMIT = 10


def _get_quota_headroom(redis_client, model_name: str) -> float:
    """Quota headroom (1 - used/limit) from the live sliding-window counters.

    Reads the RPM counter (60s TTL, maintained by the stream reader) against
    the model registry's rpm limit stored in the stats hash; falls back to
    full headroom when no data exists.
    """
    try:
        if redis_client is None:
            return 1.0

        used_rpm = int(redis_client.get(_MODEL_QUOTA_RPM_KEY.format(model=model_name)) or 0)

        # Limit lookup: seeded per-model stats hash (written at config-gen /
        # seed time); fall back to a conservative default.
        raw_limit = redis_client.hget(
            f"gateway:model:{model_name}:limits", "rpm")
        limit = int(raw_limit) if raw_limit else _DEFAULT_RPM_LIMIT

        if limit <= 0:
            return 1.0  # No quota constraint = full headroom

        headroom = 1.0 - (used_rpm / limit)
        # Clamp to [0, 1]
        return max(0.0, min(1.0, headroom))

    except Exception:
        return 1.0  # Default full headroom on error