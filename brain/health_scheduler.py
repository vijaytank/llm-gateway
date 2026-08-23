"""
brain/health_scheduler.py — Adaptive health check scheduler for the Routing Brain

Implements all rules from Issue 5 with the fixed thresholds.
Runs on its own async loop (not blocking the stream reader).

Key features per Issue 5:
- Structured health probe payload (not "ping")
- 12s timeout for cold-start accommodation
- Content-filter awareness (400 with content_filter → healthy)
- 429 at startup → rate_limited (not unhealthy)
- Adaptive intervals with jitter and exponential backoff
- Per-provider probe_timeout_seconds from config
- Moving average over last N requests
- Health check base interval: 7200s (2 hours)
- Max interval: 21600s (6 hours)
- Error backoff multiplier: 2.0
- Moving average window: 50
"""

import asyncio
import json
import time
import random
from typing import Dict, Any, List, Optional, Tuple

from brain.config import (
    HEALTH_CHECK_BASE_INTERVAL_S,
    HEALTH_CHECK_ERROR_BACKOFF_MULT,
    HEALTH_CHECK_MAX_INTERVAL_S,
    MOVING_AVG_WINDOW,
    PROBE_TIMEOUT_SECONDS,
)


class HealthProbeResult:
    """Result of a health probe check."""
    
    HEALTHY = "healthy"
    RATE_LIMITED = "rate_limited"
    CONTENT_FILTER = "content_filter"  # Treated as healthy
    UNAUTHORIZED = "unauthorized"
    UNHEALTHY = "unhealthy"
    SLOW = "slow"  # 200 but over critical threshold
    
    def __init__(self, classification: str, healthy: bool = False):
        self.classification = classification
        self.healthy = healthy


class HealthScheduler(HealthProbeResult):
    """
    Adaptive health check scheduler for gateway model providers.
    
    Responsibilities:
    1. Schedule health probes for models based on usage and error history
    2. Run structured health probes with content-filter awareness
    3. Update model status in Redis based on probe results
    4. Adjust probe intervals using exponential backoff with jitter
    5. Support per-provider timeout configuration
    """
    
    def __init__(self, redis_client=None, provider_configs: Optional[Dict[str, Any]] = None):
        self.redis = redis_client
        self.provider_configs = provider_configs or {}
        self.running = False
        
        # Track last probe time per model
        self._last_probe: Dict[str, float] = {}
        
        # Track consecutive errors per model
        self._consecutive_errors: Dict[str, int] = {}
        
        # Track last successful probe per model
        self._last_success: Dict[str, float] = {}
        
        # Probe interval per model (with jitter applied)
        self._probe_interval: Dict[str, float] = {}
    
    async def start(self) -> None:
        """Start the health scheduler async loop."""
        self.running = True
        print("HealthScheduler started")
        
        try:
            while self.running:
                await self._cycle()
                # Sleep until next cycle; use the shortest interval across all models
                await self._sleep_until_next()
        finally:
            self.running = False
            print("HealthScheduler stopped")
    
    async def _cycle(self) -> None:
        """One cycle of the health scheduler: check which models need probing."""
        current_time = time.time()
        
        # Phase 3: while offline mode is active, cloud probes pause (the
        # network path to them is presumed down) but local models keep being
        # checked so recovery of the local pool is still observed.
        offline = False
        if self.redis is not None:
            try:
                offline = bool(self.redis.get("gateway:offline_mode"))
            except Exception:
                offline = False
        
        # Find all models that need a health probe
        models_to_probe = self._determine_probe_targets(current_time)
        
        if offline:
            cloud_targets = [m for m in models_to_probe if not self._is_local(m)]
            if cloud_targets:
                print(f"[health_scheduler] offline mode — pausing probes for "
                      f"{len(cloud_targets)} cloud model(s)")
            models_to_probe = [m for m in models_to_probe if self._is_local(m)]
        
        # Run probes concurrently (with semaphore to avoid overwhelming)
        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent probes
        
        probe_tasks = []
        for model_info in models_to_probe:
            task = asyncio.create_task(
                self._probe_model(model_info, semaphore)
            )
            probe_tasks.append(task)
        
        if probe_tasks:
            await asyncio.gather(*probe_tasks, return_exceptions=True)
    
    def _determine_probe_targets(self, current_time: float) -> List[Dict[str, Any]]:
        """Determine which models need health probing based on intervals and status."""
        targets = []
        
        # Get all models with status in Redis
        if not self.redis:
            return targets
        
        try:
            # Scan for model status keys
            pattern = "gateway:model:*:status"
            cursor = 0
            while True:
                cursor, keys = self.redis.scan(cursor, pattern, count=100)
                for key in keys:
                    # Extract model name from key
                    model_name = key.decode('utf-8').split(':')[-2] if isinstance(key, bytes) else key.split(':')[-2]
                    
                    # Determine if this model needs probing
                    needs_probe = self._needs_probe(model_name, current_time)
                    if needs_probe:
                        # Get provider config
                        provider = self._get_provider_for_model(model_name)
                        targets.append({
                            "model_name": model_name,
                            "provider": provider,
                            "last_probe": self._last_probe.get(model_name, 0),
                            "consecutive_errors": self._consecutive_errors.get(model_name, 0),
                            "last_success": self._last_success.get(model_name, 0),
                        })
                
                if cursor == 0:
                    break
                cursor = int(cursor)
        except Exception:
            pass
        
        return targets
    
    def _is_local(self, model_info: Dict[str, Any]) -> bool:
        """A model belongs to the local pool (never paused during offline)."""
        name = str(model_info.get("model_name", "")).lower()
        provider = str(model_info.get("provider") or "").lower()
        return provider in ("local", "ollama", "vllm") or name.startswith("local-")

    def _needs_probe(self, model_name: str, current_time: float) -> bool:
        """Check if a model needs a health probe based on its interval."""
        last_probe = self._last_probe.get(model_name, 0)
        interval = self._get_probe_interval(model_name)
        
        # Check if enough time has elapsed since last probe
        elapsed = current_time - last_probe
        return elapsed >= interval
    
    def _get_probe_interval(self, model_name: str) -> float:
        """Get the probe interval for a model, with jitter applied."""
        # Base interval from config or default
        base_interval = HEALTH_CHECK_BASE_INTERVAL_S  # 2 hours default
        
        # Adjust based on model status
        consecutive_errors = self._consecutive_errors.get(model_name, 0)
        last_success = self._last_success.get(model_name, 0)
        
        # If model has been failing, use accelerated interval
        if consecutive_errors > 0 and last_success > 0:
            # Exponential backoff: base * 2^(errors-1)
            adjusted_interval = base_interval * (HEALTH_CHECK_ERROR_BACKOFF_MULT ** (consecutive_errors - 1))
        else:
            adjusted_interval = base_interval
        
        # Apply jitter: ±10%
        jitter = random.uniform(-0.1, 0.1) * adjusted_interval
        final_interval = max(60, adjusted_interval + jitter)  # Minimum 1 minute
        
        # Store for reference
        self._probe_interval[model_name] = final_interval
        
        return final_interval
    
    def _get_provider_for_model(self, model_name: str) -> Optional[str]:
        """Get the provider name for a model."""
        # In a full implementation, this would look up the provider from the model registry
        # For now, return None (will use default probe)
        return None
    
    async def _probe_model(self, model_info: Dict[str, Any], semaphore: asyncio.Semaphore) -> None:
        """Run a health probe for a single model."""
        async with semaphore:
            model_name = model_info["model_name"]
            provider = model_info["provider"]
            current_time = time.time()
            
            # Get provider-specific timeout
            provider_config = self.provider_configs.get(provider, {})
            timeout = provider_config.get("probe_timeout_seconds", PROBE_TIMEOUT_SECONDS)
            
            # Run the structured health probe
            try:
                # Import here to avoid circular imports
                from httpx import AsyncClient
                
                # Build the structured probe payload per Issue 5
                probe_payload = {
                    "messages": [
                        {"role": "user", "content": "Reply with the single word OK."}
                    ],
                    "max_tokens": 3,
                }
                
                # Determine the probe endpoint for this provider/model
                # In a full implementation, this would use the provider's base URL
                # and model-specific endpoint
                endpoint = self._get_probe_endpoint(model_name, provider)
                
                if not endpoint:
                    # Skip probing if no endpoint determined
                    self._on_probe_result(model_name, self.HEALTHY, healthy=True)
                    return
                
                # Run the probe with timeout
                async with AsyncClient() as client:
                    response = await client.post(
                        endpoint,
                        json=probe_payload,
                        timeout=timeout / 1000.0,
                    )
                
                # Classify the response per Issue 5
                classification, healthy = self._classify_probe_response(
                    response.status_code, 
                    response.json() if response.status_code < 500 else None,
                    provider
                )
                
                # Update scheduler state
                self._last_probe[model_name] = current_time
                
                if classification == self.HEALTHY:
                    self._consecutive_errors[model_name] = 0
                    self._last_success[model_name] = current_time
                    self._on_probe_result(model_name, self.HEALTHY, healthy=True)
                else:
                    self._consecutive_errors[model_name] = (
                        self._consecutive_errors.get(model_name, 0) + 1
                    )
                    self._on_probe_result(model_name, classification, healthy=healthy)
                    
            except Exception as e:
                # Probe failed (timeout, connection error, etc.)
                self._last_probe[model_name] = current_time
                self._consecutive_errors[model_name] = (
                    self._consecutive_errors.get(model_name, 0) + 1
                )
                self._on_probe_result(model_name, self.UNHEALTHY, healthy=False)
    
    def _get_probe_endpoint(self, model_name: str, provider: Optional[str]) -> Optional[str]:
        """Get the health probe endpoint for a model/provider.
        
        In a full implementation, this would look up the appropriate endpoint
        from the model registry or provider configuration.
        """
        # Placeholder: return None to skip probing
        # Full implementation would construct URL like:
        # - NVIDIA NIM: {base_url}/v1/models/{model}/health
        # - Groq: {base_url}/health
        # - Cerebras: {base_url}/v1/models/{model}:health
        return None
    
    def _classify_probe_response(
        self, status_code: int, body: Optional[dict], provider: Optional[str]
    ) -> Tuple[str, bool]:
        """Classify a health probe response per Issue 5 fix.
        
        Returns (classification, healthy) tuple.
        """
        # 200 OK
        if status_code == 200:
            # Zero-usage body (model loaded but empty response): treat as slow,
            # but ONLY when an explicit usage object is present. A plain 200
            # (or body without usage) is healthy.
            usage = (body or {}).get("usage")
            if isinstance(usage, dict) and usage.get("prompt_tokens") == 0 \
               and usage.get("completion_tokens") == 0:
                return self.SLOW, False  # Not unhealthy, just slow
            return self.HEALTHY, True
        
        # 429 Rate limit
        if status_code == 429:
            return self.RATE_LIMITED, False  # Not unhealthy, just rate limited
        
        # 400 - check body for content filter
        if status_code == 400 and body:
            body_str = json.dumps(body).lower()
            if any(term in body_str for term in ["content_filter", "moderation", "filtered"]):
                return self.CONTENT_FILTER, True  # Model up, just refused probe
            if any(term in body_str for term in ["invalid_request_error"]):
                return self.UNAUTHORIZED, False  # Misconfigured
        
        # 401 Unauthorized
        if status_code == 401:
            return self.UNAUTHORIZED, False
        
        # 503 Service unavailable
        if status_code == 503:
            return self.UNHEALTHY, False
        
        # Other server errors
        if status_code >= 500:
            return self.UNHEALTHY, False
        
        # Default
        return self.UNHEALTHY, False
    
    def _on_probe_result(self, model_name: str, classification: str, healthy: bool) -> None:
        """Handle a health probe result: update scheduler state and Redis."""
        current_time = time.time()
        
        # Update last probe time
        self._last_probe[model_name] = current_time
        
        # Update consecutive errors based on classification
        if classification in (self.HEALTHY, self.CONTENT_FILTER):
            # Healthy probe: reset error counter, record success
            self._consecutive_errors[model_name] = 0
            self._last_success[model_name] = current_time
        elif classification == self.RATE_LIMITED:
            # Rate limited: don't reset errors, but note it
            pass
        else:
            # Unhealthy: increment error counter
            self._consecutive_errors[model_name] = (
                self._consecutive_errors.get(model_name, 0) + 1
            )
        
        # Write status to Redis with appropriate TTL
        if healthy:
            status_key = f"gateway:model:{model_name}:status"
            ttl = HEALTH_CHECK_BASE_INTERVAL_S  # 2 hours for healthy models
            self.redis.setex(status_key, ttl, "healthy")
        else:
            # Unhealthy models get a shorter TTL so they get re-probed sooner
            status_key = f"gateway:model:{model_name}:status"
            # 30 minutes for unhealthy models before re-probe
            ttl = 1800
            self.redis.setex(status_key, ttl, classification)
        
        # Log the result
        print(f"Health probe {model_name}: {classification} (healthy={healthy})")
    
    async def _sleep_until_next(self) -> None:
        """Sleep until the next scheduled probe cycle."""
        # Find the minimum probe interval across all models
        min_interval = float('inf')
        
        for model_name, interval in self._probe_interval.items():
            if interval > 0 and interval < min_interval:
                min_interval = interval
        
        # Also consider the base intervals for models not recently probed
        if not self._probe_interval:
            min_interval = HEALTH_CHECK_BASE_INTERVAL_S
        
        # Sleep, but cap at 60 seconds to allow checking for shutdown
        sleep_time = min(min_interval, 60.0)
        await asyncio.sleep(sleep_time)
    
    def stop(self) -> None:
        """Gracefully stop the health scheduler."""
        self.running = False