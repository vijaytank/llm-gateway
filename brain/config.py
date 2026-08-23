"""
brain/config.py — Routing brain constants (Issue 5 versioned defaults)

Single source for all brain-side thresholds. Values mirror
schemas/config.py RoutingDefaults so the brain and gateway agree.
"""

# Latency thresholds (ms)
LATENCY_SLOW_THRESHOLD_MS = 8000       # mark low-priority above this
LATENCY_CRITICAL_THRESHOLD_MS = 20000  # exclude from routing above this

# Circuit breaker
CIRCUIT_BREAKER_FAILURE_COUNT = 3      # consecutive failures to open circuit
CIRCUIT_BREAKER_WINDOW_SECONDS = 300   # 5 minutes
CIRCUIT_BREAKER_COOLDOWN_429 = 600     # 10 min for rate limit
CIRCUIT_BREAKER_COOLDOWN_5XX = 1800    # 30 min for server error
CIRCUIT_BREAKER_COOLDOWN_AUTH = 86400  # 24 h for 401/403 (needs human fix)

# Score weights (must sum to 1.0)
SCORE_WEIGHT_SUCCESS_RATE = 0.40
SCORE_WEIGHT_LATENCY = 0.35
SCORE_WEIGHT_QUOTA_HEADROOM = 0.25
SCORE_WEIGHTS = {
    "success_rate": SCORE_WEIGHT_SUCCESS_RATE,
    "latency": SCORE_WEIGHT_LATENCY,
    "quota_headroom": SCORE_WEIGHT_QUOTA_HEADROOM,
}

QUOTA_DEPRIORITIZE_THRESHOLD = 0.80    # lower priority when 80% quota used

# Health checks
HEALTH_CHECK_BASE_INTERVAL_S = 7200    # 2 hours
HEALTH_CHECK_ERROR_BACKOFF_MULT = 2.0  # double on each consecutive failure
HEALTH_CHECK_MAX_INTERVAL_S = 21600    # 6 hours max

# Probe defaults (per-provider overrides come from config)
PROBE_TIMEOUT_SECONDS = 12             # cold-start accommodation

# Rolling window
MOVING_AVG_WINDOW = 50                 # last N requests for latency/success avg

# Redis keys
REDIS_STREAM_KEY = "gateway:requests:stream"
REDIS_MODEL_SCORE_KEY = "gateway:model:{name}:score"
REDIS_MODEL_STATUS_KEY = "gateway:model:{name}:status"
REDIS_MODEL_CIRCUIT_KEY = "gateway:model:{name}:circuit"
REDIS_OFFLINE_KEY = "gateway:offline_mode"
