"""
test_score_routing.py — Plan Phase 2: scores computed by the brain influence routing.

Covers (plan test_score_influences_routing):
  - Stream events processed by the live brain write score keys
    (gateway:model:<model>:score) with TTL, using the Issue 5 formula inputs
  - Success-heavy model gets a higher score than failure-heavy model
  - Rolling window is capped at moving_avg_window (50)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import gateway_chat, redis_cmd, wait_until  # noqa: E402


def _score(model):
    val = redis_cmd("get", f"gateway:model:{model}:score")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def test_successful_traffic_writes_scores():
    """Real successful requests → brain computes and stores a score in Redis."""
    for _ in range(3):
        assert gateway_chat()[0] == 200

    def _scored():
        return _score("alpha-primary") is not None
    wait_until(_scored, timeout=60, interval=1.0, desc="score key for alpha-primary")

    s = _score("alpha-primary")
    assert 0.0 <= s <= 1.0, f"score out of range: {s}"


def test_higher_success_rate_yields_higher_score():
    """Success-heavy model outscores failure-heavy model — formula behavior
    verified end-to-end through the brain's stream consumption."""
    winner, loser = "score-winner", "score-loser"
    for _ in range(6):
        redis_cmd(
            "xadd", "gateway:requests:stream", "*",
            "virtual_model", "auto-free", "actual_model", winner,
            "provider", "mock-alpha", "status", "success",
            "latency_ms", "100",
        )
    for _ in range(6):
        redis_cmd(
            "xadd", "gateway:requests:stream", "*",
            "virtual_model", "auto-free", "actual_model", loser,
            "provider", "mock-beta", "status", "failure",
            "error_code", "500", "error_type", "server_error",
        )

    def _both():
        return _score(winner) is not None and _score(loser) is not None
    wait_until(_both, timeout=90, interval=2.0, desc="both models scored")

    assert _score(winner) > _score(loser), (
        f"winner {_score(winner)} should outrank loser {_score(loser)}")


def test_rolling_window_capped_at_moving_avg_window():
    """Latency window list never exceeds moving_avg_window (50)."""
    model = "window-model"
    for i in range(60):
        redis_cmd(
            "xadd", "gateway:requests:stream", "*",
            "virtual_model", "auto-free", "actual_model", model,
            "provider", "mock-alpha", "status", "success",
            "latency_ms", str(100 + i),
        )

    def _capped():
        length = redis_cmd("llen", f"gateway:model:{model}:latency_window")
        try:
            return int(length) >= 50
        except (TypeError, ValueError):
            return False
    wait_until(_capped, timeout=120, interval=2.0,
               desc="latency window filled to cap")

    length = int(redis_cmd("llen", f"gateway:model:{model}:latency_window"))
    assert length == 50, f"window grew to {length}, cap is 50"


def test_score_key_has_ttl():
    """Scores carry a TTL so stale models decay out of routing."""
    model = "ttl-model"
    redis_cmd(
        "xadd", "gateway:requests:stream", "*",
        "virtual_model", "auto-free", "actual_model", model,
        "provider", "mock-alpha", "status", "success", "latency_ms", "100",
    )

    def _ttl():
        ttl = redis_cmd("ttl", f"gateway:model:{model}:score")
        try:
            return int(ttl) > 0
        except (TypeError, ValueError):
            return False
    wait_until(_ttl, timeout=60, interval=1.0, desc="score TTL set")
