"""
gateway/callbacks.py — LiteLLM CustomLogger writing to Redis stream

This CustomLogger hook writes per-request metadata to Postgres and publishes
events to a Redis stream that the Routing Brain consumes.

Key design decisions (Issue 5 fix — no dynamic config reload):
- Writes to Redis stream: gateway:requests:stream
- Writes metadata to Postgres request_logs table
- Does NOT modify config.yaml at runtime
- Uses XADD for append-only event stream
- Consumer group: brain:consumer group handles downstream scoring
"""

import os
import time
import json
import uuid
from typing import Optional, Dict, Any

import redis
import psycopg2
from psycopg2.extras import execute_values

# Canonical error classification (shared with the connectivity monitor).
from brain.connectivity_monitor import classify_error

# LiteLLM CustomLogger interface
# These methods are called by LiteLLM on each request completion


class RequestEvent:
    """Structured event from a LiteLLM request."""
    
    def __init__(
        self,
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
    ):
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
        self.event_id = str(uuid.uuid4())


class CustomLogger:
    """
    LiteLLM CustomLogger that:
    1. Writes request metadata to Postgres request_logs table
    2. Publishes events to Redis stream gateway:requests:stream
    """
    
    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        postgres_dsn: Optional[str] = None,
    ):
        self.redis = redis_client or redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            db=0,
            decode_responses=True,
        )
        self.postgres_dsn = postgres_dsn or os.environ.get(
            "DATABASE_URL", 
            "postgresql://llm_gateway:${POSTGRES_PASSWORD:-llm_gateway_pass}@postgres:5432/llm_gateway"
        )
        self._pg_conn = None
    
    @property
    def pg_conn(self):
        """Lazy-connect to Postgres."""
        if self._pg_conn is None or self._pg_conn.closed:
            import psycopg2
            self._pg_conn = psycopg2.connect(self.postgres_dsn)
        return self._pg_conn
    
    def log_success_event(
        self,
        virtual_model: str,
        actual_model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        ttft_ms: int,
        request_metadata: Dict[str, Any],
        response_metadata: Dict[str, Any],
    ) -> None:
        """
        Called by LiteLLM when a request succeeds.
        Writes to Postgres and Redis stream.
        """
        event = RequestEvent(
            virtual_model=virtual_model,
            actual_model=actual_model,
            provider=provider,
            status="success",
            error_code=None,
            error_type=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            routing_decision_reason="success",
            request_metadata=request_metadata,
            response_metadata=response_metadata,
        )
        
        self._write_to_postgres(event)
        self._publish_to_redis(event)
    
    def log_failure_event(
        self,
        virtual_model: str,
        actual_model: Optional[str],
        provider: Optional[str],
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        latency_ms: Optional[int] = None,
        ttft_ms: Optional[int] = None,
        request_metadata: Dict[str, Any] | None = None,
        response_metadata: Dict[str, Any] | None = None,
        status: str = "error",
    ) -> None:
        """
        Called by LiteLLM when a request fails.
        Writes to Postgres and Redis stream.

        error_type may be a raw LiteLLM exception name or HTTP status; it is
        normalized via classify_error so downstream consumers (scorer, circuit
        breaker, connectivity accounting) always see canonical types.
        """
        canonical_type = classify_error(error_code=error_code, error_type=error_type)
        event = RequestEvent(
            virtual_model=virtual_model,
            actual_model=actual_model,
            provider=provider,
            status=status,
            error_code=error_code,
            error_type=canonical_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            routing_decision_reason=f"failure:{error_type or 'unknown'}",
            request_metadata=request_metadata or {},
            response_metadata=response_metadata or {},
        )
        
        self._write_to_postgres(event)
        self._publish_to_redis(event)
    
    def _write_to_postgres(self, event: RequestEvent) -> None:
        """Write request metadata to Postgres request_logs table."""
        try:
            conn = self.pg_conn
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """INSERT INTO request_logs 
                       (id, timestamp, client_id, virtual_model, actual_model, provider, 
                        status, error_code, error_type, input_tokens, output_tokens, 
                        latency_ms, ttft_ms, routing_decision_reason, request_metadata, 
                        response_metadata)
                       VALUES %s""",
                    [(str(uuid.uuid4()), int(time.time()), event.request_metadata.get("client_id"),
                      event.virtual_model, event.actual_model, event.provider, event.status,
                      event.error_code, event.error_type, event.input_tokens, event.output_tokens,
                      event.latency_ms, event.ttft_ms, event.routing_decision_reason,
                      json.dumps(event.request_metadata), json.dumps(event.response_metadata))],
                )
            conn.commit()
        except Exception as e:
            print(f"Warning: Failed to write to Postgres: {e}")
    
    def _publish_to_redis(self, event: RequestEvent) -> None:
        """Publish event to Redis stream gateway:requests:stream."""
        try:
            # XADD format: stream * fields
            fields = {
                "event_id": event.event_id,
                "virtual_model": event.virtual_model,
                "actual_model": event.actual_model or "",
                "provider": event.provider or "",
                "status": event.status,
                "error_code": event.error_code or "",
                "error_type": event.error_type or "",
                "input_tokens": str(event.input_tokens) if event.input_tokens else "",
                "output_tokens": str(event.output_tokens) if event.output_tokens else "",
                "latency_ms": str(event.latency_ms) if event.latency_ms else "",
                "ttft_ms": str(event.ttft_ms) if event.ttft_ms else "",
                "routing_decision_reason": event.routing_decision_reason or "",
                "request_metadata": json.dumps(event.request_metadata),
                "response_metadata": json.dumps(event.response_metadata),
                "timestamp": str(int(time.time())),
            }
            
            self.redis.xadd("gateway:requests:stream", fields)
        except Exception as e:
            print(f"Warning: Failed to publish to Redis: {e}")
    
    # LiteLLM CustomLogger interface methods (optional, called on various events)
    
    def on_start(self, *args, **kwargs) -> None:
        """Called at request start — no-op by default."""
        pass
    
    def on_end(self, *args, **kwargs) -> None:
        """Called at request end — no-op by default."""
        pass
    
    def on_error(self, *args, **kwargs) -> None:
        """Called on error — no-op by default."""
        pass
    
    def on_item_start(self, *args, **kwargs) -> None:
        """Called at item start in streaming — no-op by default."""
        pass
    
    def on_item_end(self, *args, **kwargs) -> None:
        """Called at item end in streaming — no-op by default."""
        pass
    
    def on_log_start(self, *args, **kwargs) -> None:
        """Called at log start — no-op by default."""
        pass
    
    def on_log_end(self, *args, **kwargs) -> None:
        """Called at log end — no-op by default."""
        pass
    
    def terminate(self, *args, **kwargs) -> None:
        """Called at shutdown — close Postgres connection."""
        if self._pg_conn and not self._pg_conn.closed:
            self._pg_conn.close()