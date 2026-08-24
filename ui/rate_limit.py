"""
ui/rate_limit.py — Login rate limiting (Phase 5 security review)

Per plan Phase 5 deliverable 6 / test_ui_login_rate_limit:
    6 failed login attempts in 1 minute → 6th attempt returns 429.
(Plan AC states "UI login rate-limited at 5 attempts/minute".)

Design:
- Sliding window per client IP tracked in Redis (ZSET of attempt timestamps).
- Redis is already a hard dependency of the UI service, so no new infra.
- Fail-open: if Redis is unreachable, requests are allowed through —
  availability beats brute-force protection for a single-admin local UI,
  and the gateway's own auth is unaffected.

Keys: ui:login_attempts:{ip} with TTL = window; members are epoch timestamps.
"""

import time
from typing import Optional

# Tunables (plan values)
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60

_ATTEMPTS_KEY_PREFIX = "ui:login_attempts:"


class LoginRateLimiter:
    """Sliding-window rate limiter for failed login attempts, backed by Redis."""

    def __init__(self, redis_client=None,
                 max_attempts: int = LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
                 window_seconds: int = LOGIN_RATE_LIMIT_WINDOW_SECONDS):
        self.redis = redis_client
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    def _key(self, client_ip: str) -> str:
        return f"{_ATTEMPTS_KEY_PREFIX}{client_ip or 'unknown'}"

    def check_allowed(self, client_ip: str) -> bool:
        """True if this IP may attempt a login now."""
        if not self.redis:
            return True  # fail-open
        try:
            key = self._key(client_ip)
            now = time.time()
            cutoff = now - self.window_seconds
            self.redis.zremrangebyscore(key, "-inf", cutoff)
            return int(self.redis.zcard(key)) < self.max_attempts
        except Exception:
            return True  # fail-open on Redis errors

    def record_failure(self, client_ip: str) -> None:
        """Record a failed attempt and refresh the window TTL."""
        if not self.redis:
            return
        try:
            key = self._key(client_ip)
            # time.time() alone can repeat within the same clock tick, which
            # would overwrite members in the ZSET and undercount attempts.
            # Use (timestamp, auto-incrementing seq) for unique members.
            now = time.time()
            seq = self.redis.incr(f"{self._key(client_ip)}:seq")
            member = f"{now}-{seq}"
            self.redis.zadd(key, {member: now})
            self.redis.expire(key, self.window_seconds)
        except Exception:
            pass  # accounting must never break the login flow

    def reset(self, client_ip: str) -> None:
        """Clear an IP's attempts (e.g. after successful login)."""
        if not self.redis:
            return
        try:
            self.redis.delete(self._key(client_ip))
        except Exception:
            pass


def client_ip_from_request(request) -> str:
    """Extract client IP from a Starlette request (X-Forwarded-For aware)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    host = request.client.host if request.client else ""
    return host or "unknown"
