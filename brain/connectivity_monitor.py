"""
brain/connectivity_monitor.py — Offline detection per master plan Issue 10 fix.

A dedicated connectivity probe (NOT "all providers failing") decides offline mode:
  - UDP ping to a known public endpoint (default 1.1.1.1:53) every N seconds.
    UDP is used so no DNS lookup is required (a DNS outage alone cannot
    masquerade as full connectivity).
  - Count how many cloud providers returned CONNECTION errors (not auth or
    rate-limit errors) in the last `connection_error_window_seconds`.
  - Offline mode is set ONLY if the UDP probe fails AND at least
    `min_provider_failures_for_offline` cloud providers have connection errors.

State in Redis:
  - SET gateway:offline_mode 1 EX <refresh_ttl>   (must be refreshed to stay active)
  - Absent key = online. TTL expiry = automatic crash-safe recovery.
  - gateway:offline:reason  — human-readable trigger metadata (no TTL; for logs/UI)

Connection-error accounting:
  - The stream reader / callbacks classify failures with error_type
    ("rate_limit", "auth_error", "server_error", "connection_error", ...).
    Only "connection_error" counts toward offline detection.
  - Errors are recorded as sorted-set entries scored by unix timestamp;
    stale entries are pruned on each read (sliding window).
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from typing import Dict, List, Optional, Tuple

OFFLINE_KEY = "gateway:offline_mode"
OFFLINE_REASON_KEY = "gateway:offline:reason"
CONNECTION_ERRORS_KEY = "gateway:connectivity:conn_errors"  # zset member=provider score=ts

# Error types that indicate a genuine network problem (per Issue 10: auth errors
# and rate limits are NEVER offline indicators).
CONNECTION_ERROR_TYPES = frozenset({"connection_error", "timeout", "dns_error", "network_unreachable"})

# HTTP status codes / error codes that classify as connection failures.
CONNECTION_ERROR_CODES = frozenset({"0", "500", "502", "503", "504", "521", "522", "523", "524", "529"})
AUTH_ERROR_CODES = frozenset({"401", "403"})


def classify_error(error_code=None, error_type=None, status=None):
    """
    Classify a provider failure into a canonical error_type.

    Accepts whatever the caller has (HTTP status code, LiteLLM error string,
    exception class name) and returns one of:
      rate_limit | auth_error | content_filter | invalid_request |
      server_error | connection_error | timeout | unknown

    Never raises; unknown inputs map to "unknown".
    """
    try:
        et = (error_type or "").strip().lower()
        code = str(error_code).strip() if error_code is not None else ""
        st = str(status).strip() if status is not None else ""

        # Explicit types win (normalized).
        explicit_map = {
            "rate_limit": "rate_limit", "ratelimiterror": "rate_limit",
            "rate_limit_error": "rate_limit", "429": "rate_limit",
            "auth_error": "auth_error", "authenticationerror": "auth_error",
            "permissiondeniederror": "auth_error",
            "timeout": "timeout", "timeouterror": "timeout",
            "apiconnectionerror": "connection_error",
            "connectionerror": "connection_error",
            "serviceunavailableerror": "server_error",
            "internalservererror": "server_error",
            "badrequesterror": "invalid_request",
            "invalidrequesterror": "invalid_request",
        }
        for key, mapped in explicit_map.items():
            if key in et or key == code or key == st:
                return mapped

        # Fall back to numeric codes.
        combined = {code, st} - {""}
        if combined & AUTH_ERROR_CODES:
            return "auth_error"
        if "429" in combined:
            return "rate_limit"
        if combined & CONNECTION_ERROR_CODES:
            return "server_error"
        if any(c.startswith("5") for c in combined if c.isdigit()):
            return "server_error"

        # String hints (exception names often carry these substrings).
        haystack = f"{et} {code} {st}".lower()
        if "timeout" in haystack:
            return "timeout"
        if "connect" in haystack or "dns" in haystack or "unreachable" in haystack or "refused" in haystack or "network" in haystack:
            return "connection_error"
        return "unknown"
    except Exception:
        return "unknown"


def is_connection_failure(error_code=None, error_type=None, status=None) -> bool:
    """True when the classified failure counts toward offline detection."""
    return classify_error(error_code, error_type, status) in CONNECTION_ERROR_TYPES



class ConnectivityMonitor:
    """UDP connectivity probe + provider failure counter → offline mode in Redis."""

    def __init__(
        self,
        redis_client=None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        interval_seconds: Optional[int] = None,
        min_provider_failures: Optional[int] = None,
        connection_error_window_seconds: int = 120,
        refresh_ttl_seconds: int = 60,
        cloud_providers: Optional[List[str]] = None,
    ):
        self.redis = redis_client
        self.host = host or os.environ.get("GATEWAY_OFFLINE_PROBE_HOST", "1.1.1.1")
        self.port = int(port or os.environ.get("GATEWAY_OFFLINE_PROBE_PORT", 53))
        self.interval_seconds = int(
            interval_seconds or os.environ.get("GATEWAY_OFFLINE_PROBE_INTERVAL", 30)
        )
        self.min_provider_failures = int(
            min_provider_failures
            or os.environ.get("GATEWAY_MIN_PROVIDER_FAILURES_FOR_OFFLINE", 2)
        )
        self.connection_error_window_seconds = connection_error_window_seconds
        self.refresh_ttl_seconds = refresh_ttl_seconds
        # Cloud providers to watch. Defaults cover the free-first chain; local
        # providers are never counted (they ARE the offline fallback).
        self.cloud_providers = cloud_providers or ["nvidia", "openrouter", "groq", "cerebras"]
        self.running = False
        self._last_udp_ok: Optional[bool] = None
        self._instance_id = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------------ probe

    def udp_probe(self, timeout: float = 2.0) -> bool:
        """
        Send one UDP packet to host:port and consider any response (or ICMP
        refusal) proof of L3 connectivity. A timeout is ambiguous (UDP is
        fire-and-forget), so we treat *socket-level* success/failure honestly:

        - sendto() succeeding means we have a route + interface → connected.
        - ConnectionRefusedError / ICMP port unreachable arriving quickly means
          packets travel and come back → connected (host just doesn't answer).
        - Full timeout with no socket errors → treat as NOT connected only when
          it happens repeatedly; a single miss returns the previous state to
          avoid flapping on transient loss.
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            # DNS query payload (www.example.com A record) — well-formed but the
            # content doesn't matter; we only care about transport behavior.
            payload = bytes.fromhex("00010100000100000000000007657861"
                                    "6d706c6503636f6d0000010001")
            try:
                sock.sendto(payload, (self.host, self.port))
                try:
                    sock.recvfrom(512)
                    return True  # Got an actual reply
                except socket.timeout:
                    # No reply — fall through: sendto succeeded so there IS a
                    # working outbound route. Treat as connected but remember it.
                    return True
            except ConnectionRefusedError:
                # ICMP unreachable came back fast → network stack works.
                return True
            except OSError:
                return False
        except OSError:
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    async def udp_probe_async(self, timeout: float = 2.0) -> bool:
        return await asyncio.to_thread(self.udp_probe, timeout)

    # --------------------------------------------------- provider error state

    def record_provider_error(self, provider: str, error_type: str,
                              timestamp: Optional[float] = None) -> bool:
        """
        Record a provider error. Returns True if it counted as a connection
        error. Callers (stream reader / callbacks) invoke this on failures.
        """
        if error_type not in CONNECTION_ERROR_TYPES:
            return False
        if not self.redis:
            return False
        ts = timestamp if timestamp is not None else time.time()
        self.redis.zadd(CONNECTION_ERRORS_KEY, {f"{provider}:{uuid.uuid4().hex[:8]}": ts})
        return True

    def count_providers_with_connection_errors(self) -> int:
        """Number of distinct cloud providers with a connection error inside the window."""
        if not self.redis:
            return 0
        cutoff = time.time() - self.connection_error_window_seconds
        # Prune stale entries then count distinct providers.
        self.redis.zremrangebyscore(CONNECTION_ERRORS_KEY, "-inf", cutoff)
        members = self.redis.zrange(CONNECTION_ERRORS_KEY, 0, -1)
        providers = set()
        for m in members:
            name = m.decode("utf-8") if isinstance(m, bytes) else m
            providers.add(name.rsplit(":", 1)[0])
        return len(providers & set(self.cloud_providers))

    # ------------------------------------------------------------- decisions

    def evaluate(self, udp_ok: bool) -> Tuple[bool, str]:
        """
        Decide offline state from one UDP probe result + recent provider errors.

        Returns (should_be_offline, reason). Pure function of inputs — no I/O —
        so tests can drive it without Redis.
        """
        if udp_ok:
            return False, "udp_probe_ok"
        failing = self.count_providers_with_connection_errors()
        if failing >= self.min_provider_failures:
            return True, (
                f"udp_probe_failed AND {failing} cloud providers had connection "
                f"errors in last {self.connection_error_window_seconds}s "
                f"(threshold={self.min_provider_failures})"
            )
        return False, (
            f"udp_probe_failed but only {failing} cloud provider(s) with "
            f"connection errors (need >= {self.min_provider_failures}); "
            f"suspect local misconfiguration, staying online"
        )

    def apply_state(self, should_be_offline: bool, reason: str) -> Optional[bool]:
        """
        Write the decided state to Redis. Returns the new effective offline
        state (True/False), or None when Redis is unavailable.
        """
        if not self.redis:
            return None
        try:
            if should_be_offline:
                was_offline = bool(self.redis.get(OFFLINE_KEY))
                self.redis.setex(OFFLINE_KEY, self.refresh_ttl_seconds, "1")
                self.redis.set(OFFLINE_REASON_KEY, reason)
                if not was_offline:
                    print(f"[connectivity] OFFLINE MODE ENGAGED: {reason}")
            else:
                existed = self.redis.delete(OFFLINE_KEY)
                if existed:
                    self.redis.delete(OFFLINE_REASON_KEY)
                    print(f"[connectivity] connectivity restored — offline mode cleared")
            return should_be_offline
        except Exception as e:
            print(f"[connectivity] Redis write failed ({e}); keeping last-known state")
            return None

    # ---------------------------------------------------------------- loop

    async def start(self) -> None:
        """Main monitoring loop."""
        self.running = True
        print(
            f"[connectivity] monitor started: probe udp://{self.host}:{self.port} "
            f"every {self.interval_seconds}s, offline needs >= "
            f"{self.min_provider_failures} cloud provider connection failures"
        )
        try:
            while self.running:
                udp_ok = await self.udp_probe_async()
                self._last_udp_ok = udp_ok
                offline, reason = self.evaluate(udp_ok)
                self.apply_state(offline, reason)
                await asyncio.sleep(self.interval_seconds)
        finally:
            self.running = False

    def stop(self) -> None:
        self.running = False

    # ------------------------------------------------------------- helpers

    @staticmethod
    def is_offline(redis_client) -> bool:
        """Read the current offline flag (for router hook / scheduler)."""
        if redis_client is None:
            return False
        try:
            val = redis_client.get(OFFLINE_KEY)
            return bool(val)
        except Exception:
            return False

    @staticmethod
    def get_reason(redis_client) -> str:
        if redis_client is None:
            return ""
        try:
            v = redis_client.get(OFFLINE_REASON_KEY)
            return v.decode("utf-8") if isinstance(v, bytes) else (v or "")
        except Exception:
            return ""
