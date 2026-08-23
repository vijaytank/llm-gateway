"""
brain/circuit_breaker.py — Circuit breaker state machine for model routing

States: closed (normal), open (no traffic), half_open (probe allowed).
Transitions:
  closed → open: N consecutive failures within window seconds
  open → half_open: cooldown TTL expires (Redis key expiry)
  half_open → closed: next request succeeds
  half_open → open: next request fails (reset cooldown)

State stored in Redis: gateway:model:{name}:circuit with value open|half_open
(absent = closed). Key TTL = cooldown duration.

Per Issue 5 fix: All thresholds and states are versioned defaults tunable in config.
"""

import json
import time
from typing import Dict, Any, Optional, Tuple

from brain.config import (
    CIRCUIT_BREAKER_FAILURE_COUNT,
    CIRCUIT_BREAKER_WINDOW_SECONDS,
    CIRCUIT_BREAKER_COOLDOWN_429,
    CIRCUIT_BREAKER_COOLDOWN_5XX,
    CIRCUIT_BREAKER_COOLDOWN_AUTH,
)


class CircuitBreakerManager:
    """
    Circuit breaker state machine for a single model.
    
    States:
    - "closed": Normal operation. Failures are counted but don't block routing.
    - "open": Circuit open. Requests to this model are excluded.
    - "half_open": Probe allowed. One request permitted to test recovery.
    
    State is persisted in Redis with TTL-based cooldowns.
    """
    
    def __init__(self, redis_client=None):
        self.redis = redis_client
    
    def _circuit_key(self, model_name: str) -> str:
        """Get the Redis key for a model's circuit breaker state."""
        return f"gateway:model:{model_name}:circuit"
    
    def _failure_key(self, model_name: str) -> str:
        """Get the Redis key for tracking failure count."""
        return f"gateway:model:{model_name}:failure_count"
    
    def _cooldown_key(self, model_name: str, cooldown_type: str) -> str:
        """Get the Redis key for a cooldown timer."""
        return f"gateway:model:{model_name}:cooldown:{cooldown_type}"
    
    def _get_state(self, model_name: str) -> Optional[str]:
        """Get the current circuit breaker state for a model.
        
        Returns: "open", "half_open", or None (absent = closed)
        """
        if not self.redis:
            return None
        
        key = self._circuit_key(model_name)
        state = self.redis.get(key)
        
        if state is None:
            # Key absent = closed state
            return None
        
        # Decode bytes if needed
        if isinstance(state, bytes):
            state = state.decode('utf-8')
        
        return state
    
    def _set_state(self, model_name: str, state: str, ttl: Optional[int] = None) -> None:
        """Set the circuit breaker state for a model in Redis."""
        if not self.redis:
            return
        
        key = self._circuit_key(model_name)
        self.redis.set(key, state)
        
        # Set TTL if specified (for open -> half_open transition)
        if ttl is not None:
            self.redis.expire(key, ttl)
    
    def _increment_failures(self, model_name: str, ttl: int) -> None:
        """Increment the failure counter for a model."""
        if not self.redis:
            return
        
        key = self._failure_key(model_name)
        # Increment the counter; start at 1 if doesn't exist
        new_count = self.redis.incr(key)
        
        # Set TTL on the first increment
        if new_count == 1:
            self.redis.expire(key, ttl)
    
    def _reset_failures(self, model_name: str) -> None:
        """Reset the failure counter for a model."""
        if not self.redis:
            return
        
        key = self._failure_key(model_name)
        self.redis.delete(key)
    
    def get_state(self, model_name: str) -> str:
        """Get the current circuit breaker state for a model.
        
        Returns one of: "closed", "open", "half_open"
        """
        state = self._get_state(model_name)
        if state is None:
            return "closed"
        return state
    
    def record_success(self, model_name: str) -> None:
        """Record a successful request for a model.
        
        Effects:
        - Reset failure counter
        - If state was open → transition to half_open (with cooldown)
        - If state was half_open → transition to closed
        - If state was closed → stay closed
        """
        if not self.redis:
            return
        
        # Reset failure counter
        self._reset_failures(model_name)
        
        # Get current state
        current_state = self.get_state(model_name)
        
        if current_state == "open":
            # Open → half_open after cooldown TTL has elapsed
            # We set half_open state with a cooldown TTL
            cooldown = self._get_cooldown_duration(model_name)
            self._set_state(model_name, "half_open", ttl=cooldown)
            
        elif current_state == "half_open":
            # Half_open → closed on success
            self._set_state(model_name, "closed")
            
        # If closed, stay closed (no action needed)
    
    def record_auth_failure(self, model_name: str) -> None:
        """
        Record an authentication failure (401/403) for a model.

        Per Issue 5 / plan test_circuit_breaker: an auth error transitions the
        circuit to open with the 24-hour cooldown immediately — it needs a
        human fix (bad/revoked key), so retrying sooner is pointless. The
        failure counter is also reset since auth errors aren't transient.
        """
        if not self.redis:
            return

        # Open the circuit directly with the auth cooldown.
        self._set_state(model_name, "open", ttl=CIRCUIT_BREAKER_COOLDOWN_AUTH)
        self._set_cooldown(model_name, "auth", CIRCUIT_BREAKER_COOLDOWN_AUTH)
        self._reset_failures(model_name)

    def record_failure(self, model_name: str, is_429: bool = False) -> None:
        """Record a failed request for a model.
        
        Effects:
        - Increment failure counter
        - Check if threshold exceeded → transition to open
        - Different cooldowns for 429 vs 5xx vs auth errors
        """
        if not self.redis:
            return
        
        # Increment failure counter
        self._increment_failures(model_name, ttl=CIRCUIT_BREAKER_WINDOW_SECONDS)
        
        # Get current failure count
        key = self._failure_key(model_name)
        failure_count = self.redis.get(key)
        if failure_count is None:
            failure_count = 0
        if isinstance(failure_count, (bytes, str)):
            failure_count = int(failure_count)
        
        # Get current state
        current_state = self.get_state(model_name)
        
        # Determine cooldown based on error type
        if is_429:
            cooldown_seconds = CIRCUIT_BREAKER_COOLDOWN_429
            error_type = "rate_limit"
        elif current_state == "open" and self._has_recent_auth_error(model_name):
            cooldown_seconds = CIRCUIT_BREAKER_COOLDOWN_AUTH
            error_type = "auth"
        else:
            cooldown_seconds = CIRCUIT_BREAKER_COOLDOWN_5XX
            error_type = "server_error"
        
        # Check if we've hit the failure threshold to open the circuit
        if failure_count >= CIRCUIT_BREAKER_FAILURE_COUNT:
            # Open the circuit
            self._set_state(model_name, "open", ttl=cooldown_seconds)
            
            # Also set the appropriate cooldown timer
            self._set_cooldown(model_name, error_type, cooldown_seconds)
        # If not at threshold yet, stay in current state but with updated cooldown
        elif current_state == "open":
            # Renew the cooldown TTL
            self._set_cooldown(model_name, error_type, cooldown_seconds)
    
    def _has_recent_auth_error(self, model_name: str) -> bool:
        """Check if the model had an auth error recently (within the auth cooldown window)."""
        # Simplified check: if the cooldown key exists and hasn't expired
        cooldown_key = self._cooldown_key(model_name, "auth")
        if self.redis and self.redis.exists(cooldown_key):
            # Check TTL - if TTL > 0, it's still active
            ttl = self.redis.ttl(cooldown_key)
            return ttl > 0
        return False
    
    def _set_cooldown(self, model_name: str, cooldown_type: str, seconds: int) -> None:
        """Set a cooldown timer for a model."""
        if not self.redis:
            return
        
        key = self._cooldown_key(model_name, cooldown_type)
        self.redis.setex(key, seconds, "1")
    
    def transition_to_closed(self, model_name: str) -> None:
        """Transition the circuit breaker to closed state explicitly.
        
        Used when a manual recovery is desired or after a probe succeeds.
        """
        if not self.redis:
            return
        
        self._set_state(model_name, "closed")
        self._reset_failures(model_name)
        # Remove any cooldown timers
        if self.redis:
            for ctype in ["429", "5xx", "auth"]:
                self.redis.delete(self._cooldown_key(model_name, ctype))
    
    def check_half_open_probe_allowed(self, model_name: str) -> bool:
        """Check if a half-open probe is allowed for this model.
        
        Returns True if:
        - State is half_open AND
        - The cooldown TTL has NOT yet expired (probe is still within its window)
        
        Returns False if:
        - State is not half_open, OR
        - The cooldown TTL has expired (it's time to close the circuit)
        """
        state = self.get_state(model_name)
        if state != "half_open":
            return False
        
        # Check if the half-open cooldown TTL has expired
        # The half_open state was set with a cooldown TTL
        circuit_key = self._circuit_key(model_name)
        if self.redis:
            ttl = self.redis.ttl(circuit_key)
            # If TTL <= 0, the cooldown has expired → should close the circuit
            return ttl > 0
        
        return False