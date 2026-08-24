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
from datetime import datetime, timezone
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


try:
    from litellm.integrations.custom_logger import CustomLogger as _LiteLLMCustomLogger
except ImportError:  # litellm not installed (e.g. host unit-test env)
    _LiteLLMCustomLogger = object


class CustomLogger(_LiteLLMCustomLogger):
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
        self.redis = redis_client or (
            redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
            if os.environ.get("REDIS_URL")
            else redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", 6379)),
                db=0,
                decode_responses=True,
            )
        )
        # GATEWAY_DB_URL preferred: a bare DATABASE_URL in the gateway process
        # makes LiteLLM enable its Prisma layer (which resets the schema and
        # wipes our tables). See docker-compose notes.
        self.postgres_dsn = (
            postgres_dsn
            or os.environ.get("GATEWAY_DB_URL")
            or os.environ.get("DATABASE_URL")
            or "postgresql://llm_gateway:***@postgres:5432/llm_gateway"
        )
        self._pg_conn = None
    
    @property
    def pg_conn(self):
        """Lazy-connect to Postgres."""
        if self._pg_conn is None or self._pg_conn.closed:
            import psycopg2
            self._pg_conn = psycopg2.connect(self.postgres_dsn)
        return self._pg_conn
    
    def record_success(
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
    
    def record_failure(
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
                    [(str(uuid.uuid4()), datetime.fromtimestamp(int(time.time()), tz=timezone.utc), event.request_metadata.get("client_id"),
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

    # ------------------------------------------------------------------
    # LiteLLM hook interface. LiteLLM calls log/async_log_*_event with
    # (kwargs, response_obj, start_time, end_time); these adapters translate
    # into the domain methods above so routing code stays framework-free.
    # ------------------------------------------------------------------

    def _model_info(self, kwargs: Dict[str, Any], response_obj: Any = None) -> Dict[str, Any]:
        kwargs = kwargs or {}
        model_obj = kwargs.get("litellm_params") or {}
        model_info = getattr(model_obj, "model_info", None) or (
            model_obj.get("model_info") if isinstance(model_obj, dict) else None) or {}
        metadata = kwargs.get("litellm_metadata") or kwargs.get("metadata") or {}
        # Actual deployed model: prefer the concrete deployment string; the
        # proxy's model_info.model_name is the virtual group, not the upstream.
        actual = None
        resp_model = getattr(response_obj, "model", None)
        if isinstance(resp_model, str) and resp_model:
            actual = resp_model.split("/")[-1]
        if not actual:
            lp_model = getattr(model_obj, "model", None) or (
                model_obj.get("model") if isinstance(model_obj, dict) else None)
            if isinstance(lp_model, str) and lp_model:
                actual = lp_model.split("/")[-1]
        return {
            "virtual_model": kwargs.get("model") or metadata.get("model_group") or "",
            "actual_model": actual or "",
            "provider": (model_info.get("provider") if isinstance(model_info, dict) else "") or "",
        }

    def _latency_ms(self, start_time, end_time) -> int:
        if start_time and end_time:
            try:
                return int((end_time - start_time).total_seconds() * 1000)
            except Exception:
                return 0
        return 0

    def _on_success(self, kwargs, response_obj, start_time, end_time) -> None:
        info = self._model_info(kwargs, response_obj=response_obj)
        usage = getattr(response_obj, "usage", None)
        self.record_success(
            virtual_model=info["virtual_model"],
            actual_model=info["actual_model"],
            provider=info["provider"],
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=self._latency_ms(start_time, end_time),
            ttft_ms=0,
            request_metadata={"model_group": (kwargs or {}).get("model")},
            response_metadata={},
        )

    def _on_failure(self, kwargs, response_obj, start_time, end_time) -> None:
        info = self._model_info(kwargs)
        exc = (kwargs or {}).get("exception")
        status_code = getattr(exc, "status_code", None)
        self.record_failure(
            virtual_model=info["virtual_model"],
            actual_model=info["actual_model"],
            provider=info["provider"],
            error_code=str(status_code) if status_code else None,
            error_type=exc.__class__.__name__ if exc else None,
            latency_ms=self._latency_ms(start_time, end_time),
            request_metadata={"model_group": (kwargs or {}).get("model")},
            response_metadata={},
        )

    def _latency_ms(self, start_time, end_time) -> int:
        if start_time and end_time:
            try:
                return int((end_time - start_time).total_seconds() * 1000)
            except Exception:
                return 0
        return 0

    def _on_success(self, kwargs, response_obj, start_time, end_time) -> None:
        info = self._model_info(kwargs, response_obj=response_obj)
        usage = getattr(response_obj, "usage", None)
        self.record_success(
            virtual_model=info["virtual_model"],
            actual_model=info["actual_model"],
            provider=info["provider"],
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=self._latency_ms(start_time, end_time),
            ttft_ms=0,
            request_metadata={"model_group": (kwargs or {}).get("model")},
            response_metadata={},
        )

    def _on_failure(self, kwargs, response_obj, start_time, end_time) -> None:
        info = self._model_info(kwargs)
        exc = (kwargs or {}).get("exception")
        status_code = getattr(exc, "status_code", None)
        self.record_failure(
            virtual_model=info["virtual_model"],
            actual_model=info["actual_model"],
            provider=info["provider"],
            error_code=str(status_code) if status_code else None,
            error_type=exc.__class__.__name__ if exc else None,
            latency_ms=self._latency_ms(start_time, end_time),
            request_metadata={"model_group": (kwargs or {}).get("model")},
            response_metadata={},
        )

    # Async hooks: the Postgres write is blocking psycopg2 I/O — run it on a
    # worker thread so LiteLLM's event loop is never stalled per-request
    # (review F-M8). The Redis XADD is likewise offloaded.
    async def _record_success_async(self, kwargs, response_obj, start_time, end_time):
        import asyncio
        await asyncio.to_thread(self._on_success, kwargs, response_obj, start_time, end_time)

    async def _record_failure_async(self, kwargs, response_obj, start_time, end_time):
        import asyncio
        await asyncio.to_thread(self._on_failure, kwargs, response_obj, start_time, end_time)

    # ------------------------------------------------------------------
    # Pre-call routing hook (review F-H1): LiteLLM calls async_pre_call_hook
    # before model selection. We consult the brain's Redis state via
    # RouterHook.influence_model_selection() and translate it into
    # LiteLLM-native signals on the request metadata:
    #   influence == -1  -> metadata["gateway_model_excluded"] = True
    #                       (router_hook also mirrors to cooldown keys below)
    #   otherwise        -> metadata["gateway_influence"] = <score>
    # The generated config enables router_settings.routing_strategy
    # "latency-based-routing-v2" plus our cooldown mirroring, so excluded
    # deployments are skipped natively by the proxy.
    # ------------------------------------------------------------------

    def _router_hook(self):
        """Lazily construct the RouterHook (shares this logger's Redis)."""
        if getattr(self, "_router_hook_instance", None) is None:
            try:
                from gateway.router_hook import RouterHook
                self._router_hook_instance = RouterHook(redis_client=self.redis)
            except Exception as e:
                print(f"[callbacks] router hook unavailable: {e}")
                self._router_hook_instance = False  # sentinel: don't retry per request
        return self._router_hook_instance or None

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Consult live routing state before LiteLLM picks a deployment."""
        try:
            hook = self._router_hook()
            if hook is None:
                return data
            virtual_model = (data or {}).get("model", "")
            fallback_chain = [virtual_model]
            influence, reason = hook.influence_model_selection(
                virtual_model, fallback_chain)
            if not isinstance(data.get("metadata"), dict):
                data["metadata"] = {}
            metadata = data["metadata"]
            metadata["gateway_influence"] = influence
            metadata["gateway_routing_reason"] = reason
            if influence < 0:
                # Excluded: record so post-call accounting can attribute the
                # fallback; LiteLLM's own cooldown for this deployment is set
                # by the brain's circuit writer (Redis key already open).
                metadata["gateway_model_excluded"] = True
        except Exception as e:
            # Fail-safe: never block a request because routing introspection failed.
            print(f"[callbacks] pre-call routing check failed: {e}")
        return data

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM sync success hook."""
        try:
            self._on_success(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            print(f"Warning: success hook failed: {e}")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM async success hook (non-blocking: DB write off the loop)."""
        try:
            await self._record_success_async(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            print(f"Warning: async success hook failed: {e}")

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM sync failure hook."""
        try:
            self._on_failure(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            print(f"Warning: failure hook failed: {e}")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM async failure hook (non-blocking: DB write off the loop)."""
        try:
            await self._record_failure_async(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            print(f"Warning: async failure hook failed: {e}")


# Module-level instance LiteLLM imports by dotted path (see gateway/main.py:
# LITELLM_CUSTOM_CALLBACKS defaults to "gateway.callbacks.custom_logger").
# Lazy-init pattern: constructing CustomLogger opens no sockets (redis client
# and Postgres connection are created on first use), so import-time
# instantiation is safe in any process that merely imports this module.
custom_logger = CustomLogger()