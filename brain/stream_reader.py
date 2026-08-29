"""
brain/stream_reader.py — Redis XREAD consumer loop for the Routing Brain

Reads events from the gateway:requests:stream Redis stream as a consumer group,
parses each event into a structured RequestEvent object, and dispatches to the
score updater and circuit breaker manager.

Design (per master plan Issue 9):
- Separate Python process running alongside LiteLLM
- Same Docker service, managed by supervisord
- Reads from Redis stream; does NOT modify config.yaml at runtime
- Uses XREADGROUP with XACK for exactly-once processing
"""

import json
import os
import sys
import time
import uuid
import asyncio
from typing import Dict, Any, List, Optional, Tuple

import redis

from brain.scorer import compute_score, SCORE_WEIGHTS
from brain.circuit_breaker import CircuitBreakerManager
from brain.connectivity_monitor import (
    ConnectivityMonitor,
    classify_error,
    is_connection_failure,
)
from schemas.db import RequestLog, ModelRegistry


class RequestEvent:
    """Structured event from a LiteLLM request via Redis stream."""
    
    def __init__(
        self,
        event_id: str,
        virtual_model: str,
        actual_model: Optional[str],
        provider: Optional[str],
        status: str,
        error_code: Optional[str],
        error_type: Optional[str],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        latency_ms: Optional[int],
        ttft_ms: Optional[int],
        routing_decision_reason: Optional[str],
        request_metadata: Dict[str, Any],
        response_metadata: Dict[str, Any],
        timestamp: float,
    ):
        self.event_id = event_id
        self.virtual_model = virtual_model
        self.actual_model = actual_model
        self.provider = provider
        self.status = status
        self.error_code = error_code
        self.error_type = error_type
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.ttft_ms = ttft_ms
        self.routing_decision_reason = routing_decision_reason
        self.request_metadata = request_metadata
        self.response_metadata = response_metadata
        self.timestamp = timestamp


class StreamReader:
    """
    Redis XREADGROUP consumer loop for the routing brain.
    
    Responsibilities:
    1. Consume events from gateway:requests:stream consumer group
    2. Parse each event into RequestEvent
    3. Dispatch to score updater and circuit breaker manager
    4. Handle Ack/Nack of processed messages
    5. Support graceful shutdown
    """
    
    def __init__(
        self,
        redis_client: redis.Redis,
        stream_name: str = "gateway:requests:stream",
        consumer_group: str = "brain:consumer",
        consumer_name: str = f"brain-node-{uuid.uuid4().hex[:8]}",
        score_ttl_seconds: int = 300,  # 5-minute TTL for scores
        window_size: int = 50,  # Rolling window for scoring
        connection_error_window_seconds: int = 120,  # offline detection window
        claim_idle_ms: int = 60_000,  # XAUTOCLAIM idle threshold
    ):
        self.redis = redis_client
        from brain.scorer import set_redis_client
        if redis_client is not None:
            set_redis_client(redis_client)
        self.stream_name = stream_name
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name
        self.score_ttl = score_ttl_seconds
        self.window_size = window_size
        self.claim_idle_ms = claim_idle_ms
        self.running = False
        self.message_counter = 0
        # One shared circuit-breaker manager: used by event handling AND the
        # metrics gauge (previously the gauge read a never-set attribute and
        # always reported "closed").
        from brain.circuit_breaker import CircuitBreakerManager
        self.cb_manager = CircuitBreakerManager(redis_client)
        # Connectivity error accounting (Phase 3): every failure event that
        # classifies as a connection failure is recorded here so the
        # connectivity monitor's provider-failure count updates automatically.
        self.connectivity_monitor = ConnectivityMonitor(
            redis_client=redis_client,
            connection_error_window_seconds=connection_error_window_seconds,
        )
    
    def start(self) -> None:
        """Start the stream reader loop."""
        self.running = True
        
        # Ensure consumer group exists
        self._ensure_consumer_group()
        
        # Recover pending messages from a previous crashed consumer before
        # entering the main loop (plan Phase 2 AC: XAUTOCLAIM handles unacked
        # messages after a brain crash). Claim anything idle past the
        # threshold and process it like a fresh message.
        try:
            self._reclaim_pending()
        except Exception as e:
            print(f"[stream_reader] pending reclaim skipped: {e}")
        
        print(f"StreamReader started: consumer={self.consumer_name}, "
              f"stream={self.stream_name}, group={self.consumer_group}")
        
        try:
            self._read_loop()
        finally:
            self.running = False
            print("StreamReader stopped")
    
    def _ensure_consumer_group(self) -> None:
        """Ensure the Redis consumer group exists; create if not."""
        try:
            self.redis.xgroup_create(
                name=self.stream_name,
                groupname=self.consumer_group,
                id='0',  # Start from the beginning
                mkstream=True,
            )
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" in str(e).upper():
                print(f"Consumer group {self.consumer_group} already exists")
            else:
                raise
    
    def _reclaim_pending(self, batch: int = 50) -> int:
        """XAUTOCLAIM messages stuck pending (crashed consumer), then process them.

        Returns the number of reclaimed-and-processed messages.
        """
        cursor = "0-0"
        claimed_total = 0
        while True:
            result = self.redis.xautoclaim(
                self.stream_name,
                self.consumer_group,
                self.consumer_name,
                min_idle_time=self.claim_idle_ms,
                start_id=cursor,
                count=batch,
            )
            # redis-py returns (next_cursor, messages[, deleted_ids]) — older
            # versions return just the pair; handle both shapes.
            next_cursor = result[0]
            messages = result[1] if len(result) > 1 else []
            for entry in messages:
                message_id, message_data = entry[0], entry[1]
                try:
                    self._process_message(message_id, message_data)
                    claimed_total += 1
                except Exception as e:
                    print(f"Error processing reclaimed message {message_id}: {e}")
            if next_cursor == "0-0" or not messages:
                break
            cursor = next_cursor
        if claimed_total:
            print(f"[stream_reader] reclaimed {claimed_total} pending message(s)")
        return claimed_total
    
    def _read_loop(self) -> None:
        """Main read loop: XREADGROUP with BLOCK, process events, ACK."""
        print("Entering stream read loop...")
        
        while self.running:
            try:
                # XREADGROUP with BLOCK 5000ms (5s timeout)
                results = self.redis.xreadgroup(
                    self.consumer_group,
                    self.consumer_name,
                    {self.stream_name: ">"},  # > means new messages only
                    count=10,  # max 10 messages per poll
                    block=5000,  # 5 seconds timeout
                )
                
                if not results:
                    continue
                
                for stream, messages in results:
                    for message_id, message_data in messages:
                        try:
                            self._process_message(message_id, message_data)
                        except Exception as e:
                            print(f"Error processing message {message_id}: {e}")
                            # Not ACKed — stays pending; XAUTOCLAIM reclaims it later
                        
            except Exception as e:
                if self.running:
                    print(f"Stream read error: {e}")
                time.sleep(1)  # Brief pause before retry
    
    def _process_message(self, message_id: str, message_data: Dict[str, Any]) -> None:
        """Process a single stream message: parse, score, circuit breaker, then ACK.

        ACK comes LAST (process-then-ack): a crash mid-processing leaves the
        message pending instead of silently lost (plan: exactly-once intent).
        """
        try:
            # Parse the event data FIRST
            event = self._parse_event(message_data)
            
            if event is None:
                # Poison message: can never parse. Acknowledge so it doesn't
                # block the pending list forever, but log loudly.
                print(f"Failed to parse message {message_id} — acknowledging poison message")
                self.redis.xack(self.stream_name, self.consumer_group, message_id)
                return
            
            # Dispatch to score updater
            self._update_score(event)
            
            # Dispatch to circuit breaker manager
            self._update_circuit_breaker(event)

            # Phase 5: feed Prometheus counters/gauges (never blocks).
            metrics = getattr(self, "metrics", None)
            if metrics is not None:
                try:
                    model_for_metrics = event.actual_model or event.virtual_model
                    metrics.observe_request(
                        model=model_for_metrics,
                        provider=event.provider or "unknown",
                        status=event.status,
                        latency_ms=event.latency_ms,
                    )
                    metrics.set_circuit_state(
                        model_for_metrics,
                        self.cb_manager.get_state(model_for_metrics),
                    )
                except Exception as me:
                    print(f"[metrics] update failed: {me}")

            # Automatic connectivity error accounting (Phase 3): failures from
            # cloud providers that classify as connection failures feed the
            # connectivity monitor's offline-detection window.
            self._record_connectivity_error(event)
            
            # Log processing stats
            self.message_counter += 1
            if self.message_counter % 100 == 0:
                print(f"Processed {self.message_counter} events...")
            
            # ACK only AFTER successful processing.
            self.redis.xack(self.stream_name, self.consumer_group, message_id)
                
        except Exception as e:
            print(f"Failed to process message {message_id}: {e}")
            # No ACK — message stays pending; XAUTOCLAIM will retry it later.
    
    def _parse_event(self, message_data: Dict[str, Any]) -> Optional[RequestEvent]:
        """Parse raw Redis message data into RequestEvent."""
        try:
            # Redis XADD fields are stored as strings; convert as needed
            return RequestEvent(
                event_id=message_data.get("event_id", str(uuid.uuid4())),
                virtual_model=message_data.get("virtual_model", ""),
                actual_model=message_data.get("actual_model") or None,
                provider=message_data.get("provider") or None,
                status=message_data.get("status", "unknown"),
                error_code=message_data.get("error_code") or None,
                error_type=message_data.get("error_type") or None,
                input_tokens=int(message_data.get("input_tokens", 0)) if message_data.get("input_tokens") else None,
                output_tokens=int(message_data.get("output_tokens", 0)) if message_data.get("output_tokens") else None,
                latency_ms=int(message_data.get("latency_ms", 0)) if message_data.get("latency_ms") else None,
                ttft_ms=int(message_data.get("ttft_ms", 0)) if message_data.get("ttft_ms") else None,
                routing_decision_reason=message_data.get("routing_decision_reason") or None,
                request_metadata=json.loads(message_data.get("request_metadata", "{}")),
                response_metadata=json.loads(message_data.get("response_metadata", "{}")),
                timestamp=float(message_data.get("timestamp", time.time())),
            )
        except (ValueError, TypeError, KeyError) as e:
            print(f"Error parsing event: {e}")
            return None
    
    def _update_score(self, event: RequestEvent) -> None:
        """Update model score using the scoring formula.

        Maintains three Redis structures per model:
        - outcome_window: rolling last-N list of 1/0 outcomes (success rate
          over the moving window per plan Issue 5 — replaces the old unbounded
          cumulative successes/failures hash)
        - score: computed score with TTL (router sort key)
        - latency_window + quota counters (rpm/rpd sliding windows)
        """
        try:
            # Determine which model to score (actual_model takes priority, fallback to virtual)
            model_name = event.actual_model or event.virtual_model
            if not model_name:
                return
            
            # Record the outcome in the ROLLING window first so the scorer's
            # success-rate term reflects the last N requests, not all history.
            outcome_key = f"gateway:model:{model_name}:outcome_window"
            pipe = self.redis.pipeline()
            pipe.rpush(outcome_key, "1" if event.status == "success" else "0")
            pipe.ltrim(outcome_key, -self.window_size, -1)
            pipe.expire(outcome_key, self.score_ttl * 6)

            # Quota counters (sliding windows): RPM counter with a 60s TTL and
            # an RPD zset trimmed to the current UTC day. These feed the
            # quota_headroom term of compute_score (previously constant 1.0).
            now = time.time()
            rpm_key = f"gateway:model:{model_name}:quota:rpm"
            rpd_key = f"gateway:model:{model_name}:quota:rpd"
            pipe.incr(rpm_key)
            pipe.expire(rpm_key, 60)
            pipe.zadd(rpd_key, {f"{now}": now})
            pipe.zremrangebyscore(rpd_key, "-inf", now - 86400)
            pipe.expire(rpd_key, 86400)
            pipe.execute()
            
            # Compute new score using the formula from Issue 5
            new_score = compute_score(
                model_name=model_name,
                provider=event.provider or "unknown",
                status=event.status,
                latency_ms=event.latency_ms,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                window_size=self.window_size,
            )
            
            # Write score to Redis with TTL
            score_key = f"gateway:model:{model_name}:score"
            self.redis.setex(
                score_key,
                self.score_ttl,
                new_score,
            )
            
            # Maintain a rolling latency window in Redis (list capped at window_size)
            latency_key = f"gateway:model:{model_name}:latency_window"
            pipe = self.redis.pipeline()
            pipe.rpush(latency_key, event.latency_ms or 0)
            pipe.ltrim(latency_key, -self.window_size, -1)
            pipe.expire(latency_key, self.score_ttl * 2)
            pipe.execute()
            
        except Exception as e:
            print(f"Error updating score for model {event.actual_model or event.virtual_model}: {e}")
    
    def _record_connectivity_error(self, event: RequestEvent) -> None:
        """
        Feed failure events into connectivity error accounting.

        - Skips local-pool events (they are the offline fallback, never
          indicators of internet loss).
        - Classifies the failure from error_type/error_code; unknown types get
          classified automatically so upstream callers don't need to be exact.
        - Records only genuine connection failures (never auth/rate-limit).
        """
        try:
            if event.status == "success":
                return

            provider = (event.provider or "").strip().lower()
            if provider in ("local", "ollama", "vllm", ""):
                return

            model_name = event.actual_model or event.virtual_model or ""
            if model_name.lower().startswith("local-"):
                return

            # Normalize the classification before testing it — an event with
            # error_type="unknown" but error_code=503 is a server/connection
            # class failure, and a timeout exception name maps to timeout.
            canonical = classify_error(
                error_code=event.error_code,
                error_type=event.error_type,
            )
            if not is_connection_failure(error_type=canonical):
                return

            recorded_at = event.timestamp if event.timestamp else None
            counted = self.connectivity_monitor.record_provider_error(
                provider, canonical, timestamp=recorded_at
            )
            if counted:
                print(f"[stream_reader] connection failure recorded: "
                      f"provider={provider} type={canonical}")
        except Exception as e:
            # Accounting must never break stream processing.
            print(f"[stream_reader] connectivity accounting skipped: {e}")

    def _update_circuit_breaker(self, event: RequestEvent) -> None:
        """Update circuit breaker state based on request outcome."""
        try:
            model_name = event.actual_model or event.virtual_model
            if not model_name:
                return
            
            from brain.circuit_breaker import CircuitBreakerManager
            cb = CircuitBreakerManager(self.redis)

            if event.status == "success":
                # Success: move toward closed state
                cb.transition_to_closed(model_name)
                return
            if event.status != "error":
                return
            
            # Canonicalize the error type so auth errors (401/403) get the
            # 24-hour cooldown per plan test_circuit_breaker, regardless of
            # whether the caller sent "auth_error", a raw code, or an
            # exception name.
            from brain.connectivity_monitor import classify_error
            canonical = classify_error(
                error_code=event.error_code, error_type=event.error_type)
            
            if canonical == "auth_error":
                # Auth failure: open immediately with the 24-hour cooldown.
                cb.record_auth_failure(model_name)
            elif canonical == "rate_limit":
                # Rate limit: increment failure count with 429 cooldown path
                cb.record_failure(model_name, is_429=True)
            else:
                # Other transient errors: increment failure count (5xx path)
                cb.record_failure(model_name, is_429=False)

            # Phase 5: if this model's circuit just opened, feed provider-level
            # accounting — 3+ models opening within 5 min deprioritizes the
            # whole provider (no full circuit trip on remaining models).
            if cb.get_state(model_name) == "open":
                from brain.provider_circuit import ProviderCircuitManager
                ProviderCircuitManager(self.redis).record_model_circuit_open(
                    event.provider or "", model_name)

        except Exception as e:
            print(f"Error updating circuit breaker for model {model_name}: {e}")
    
    def stop(self) -> None:
        """Gracefully stop the stream reader."""
        self.running = False


# For CLI usage
def main():
    """CLI entry point for running the stream reader."""
    import argparse
    
    parser = argparse.ArgumentParser(description="LLM Gateway Routing Brain Stream Reader")
    parser.add_argument("--redis-host", type=str, default=os.environ.get("REDIS_HOST", "localhost"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", 6379)))
    parser.add_argument("--db-url", type=str, default=os.environ.get("DATABASE_URL", "postgresql://localhost/llm_gateway"))
    args = parser.parse_args()
    
    # Connect to Redis
    redis_client = redis.Redis(
        host=args.redis_host,
        port=args.redis_port,
        decode_responses=True,
    )
    
    # Verify connection
    try:
        redis_client.ping()
        print("Connected to Redis successfully")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")
        return 1
    
    # Start the stream reader
    reader = StreamReader(redis_client=redis_client)
    print("Starting stream reader...")
    reader.start()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())