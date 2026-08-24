"""
GatewayConfig — Canonical configuration schema for the LLM Gateway.

This is the single source of truth for all configuration. All writers (wizard, UI, CLI)
serialize a GatewayConfig instance to YAML. All readers (gateway, brain, adapter)
validate against this model at startup.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.types import SecretStr


class ConnectivityConfig(BaseModel):
    """Network connectivity probe settings for offline detection."""
    offline_probe_host: str = Field(default="1.1.1.1", description="Host to probe for connectivity (UDP)")
    offline_probe_port: int = Field(default=53, ge=1, le=65535, description="Port for UDP connectivity probe")
    offline_probe_interval_seconds: int = Field(default=30, ge=5, le=300, description="Interval between connectivity probes")
    min_provider_failures_for_offline: int = Field(default=2, ge=1, le=10, description="Minimum cloud providers with connection errors to trigger offline mode")


class ProviderConfig(BaseModel):
    """Per-provider configuration."""
    enabled: bool = True
    tier: Literal["free", "premium"] = "free"
    probe_timeout_seconds: int = Field(default=12, ge=5, le=60, description="Timeout for health probes to this provider")
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # OpenRouter-specific
    warn_on_charge_risk: bool = False
    effective_rpd: Optional[int] = Field(default=None, ge=1, description="Conservative RPD limit for charge-risk providers")


class ProvidersConfig(BaseModel):
    """All provider configurations."""
    nvidia: ProviderConfig = Field(default_factory=lambda: ProviderConfig(enabled=True, tier="free", probe_timeout_seconds=12))
    openrouter: ProviderConfig = Field(
        default_factory=lambda: ProviderConfig(
            enabled=True,
            tier="free",
            warn_on_charge_risk=True,
            effective_rpd=40,
            extra_headers={"HTTP-Referer": "http://localhost:4000", "X-Title": "llm-gateway"},
        )
    )
    groq: ProviderConfig = Field(default_factory=lambda: ProviderConfig(enabled=True, tier="free"))
    cerebras: ProviderConfig = Field(default_factory=lambda: ProviderConfig(enabled=True, tier="free"))
    local: ProviderConfig = Field(
        default_factory=lambda: ProviderConfig(
            enabled=False,
            tier="free",
            extra_headers={},
        )
    )
    # Local provider extra fields
    ollama_base_url: str = "http://localhost:11434"
    vllm_base_url: str = ""


class VirtualModelConfig(BaseModel):
    """Virtual model definition (what clients see)."""
    name: str
    capabilities: list[str] = Field(default_factory=list)
    tier: Literal["free", "premium"] = "free"
    fallback_chain: list[str] = Field(default_factory=list)


class CustomProviderConfig(BaseModel):
    """A user-defined OpenAI-compatible provider added via wizard or UI.

    No hardcoded endpoints: name, base_url, and models are all user-supplied
    (or discovered via a /v1/models ping at add time).
    """
    name: str = Field(min_length=1, max_length=64, description="Unique provider identifier (lowercase, no spaces)")
    base_url: str = Field(min_length=1, description="OpenAI-compatible base URL, e.g. https://host/v1")
    api_key_env: str = Field(default="", description="Env var holding the API key (empty = no auth)")
    auth_type: Literal["none", "bearer", "header"] = "bearer"
    tier: Literal["free", "premium"] = "free"
    enabled: bool = True
    probe_timeout_seconds: int = Field(default=12, ge=5, le=60)
    models: list[str] = Field(min_length=1, description="Upstream model names served by this provider")
    capabilities: list[str] = Field(default_factory=list)
    rpm: int = Field(default=10, ge=1, description="Conservative RPM limit used in LiteLLM params")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError(
                f"provider name '{v}' must be lowercase alphanumeric with - or _ only"
            )
        return v

    @field_validator("models")
    @classmethod
    def validate_models(cls, v: list, info) -> list:
        # Sentinel placeholders used transiently during UI probing must NEVER
        # reach a persisted config or a LiteLLM deployment (review F-M7).
        for m in v:
            if isinstance(m, str) and m.startswith("__"):
                raise ValueError(
                    f"placeholder model '{m}' cannot be persisted; resolve real "
                    "model names via discovery or manual entry first"
                )
        if not v:
            raise ValueError("at least one model name is required")
        return v

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"base_url must start with http:// or https://, got '{v}'")
        return v.rstrip("/")


class LiteLLMSettings(BaseModel):
    """LiteLLM proxy settings."""
    cache: bool = True
    cache_params: dict[str, Any] = Field(
        default_factory=lambda: {"type": "redis", "host": "${REDIS_HOST}", "port": 6379}
    )


class RoutingDefaults(BaseModel):
    """Routing scoring and circuit breaker thresholds (all tunable)."""
    latency_slow_threshold_ms: int = Field(default=8000, ge=100, description="Mark as low-priority above this latency")
    latency_critical_threshold_ms: int = Field(default=20000, ge=1000, description="Exclude from routing above this latency")
    circuit_breaker_failure_count: int = Field(default=3, ge=1, le=10, description="Consecutive failures to open circuit")
    circuit_breaker_window_seconds: int = Field(default=300, ge=30, le=3600, description="Window for failure counting")
    cooldown_429_seconds: int = Field(default=600, ge=60, le=86400, description="Cooldown for rate limit (429)")
    cooldown_5xx_seconds: int = Field(default=1800, ge=60, le=86400, description="Cooldown for server errors (5xx)")
    cooldown_auth_seconds: int = Field(default=86400, ge=3600, le=604800, description="Cooldown for auth errors (401/403)")
    score_weight_success_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    score_weight_latency: float = Field(default=0.35, ge=0.0, le=1.0)
    score_weight_quota_headroom: float = Field(default=0.25, ge=0.0, le=1.0)
    quota_deprioritize_threshold: float = Field(default=0.80, ge=0.0, le=1.0, description="Lower priority when quota used > this")
    health_check_base_interval_seconds: int = Field(default=7200, ge=300, le=86400, description="Base interval for health checks")
    health_check_error_backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0, description="Backoff multiplier per consecutive error")
    health_check_max_interval_seconds: int = Field(default=21600, ge=3600, le=604800, description="Max health check interval")
    moving_avg_window: int = Field(default=50, ge=10, le=1000, description="Rolling window for latency/success averages")

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "RoutingDefaults":
        total = self.score_weight_success_rate + self.score_weight_latency + self.score_weight_quota_headroom
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Score weights must sum to 1.0, got {total}")
        return self


class GeneralSettings(BaseModel):
    """General gateway settings."""
    master_key: SecretStr = Field(
        default_factory=lambda: SecretStr(f"sk-litellm-{secrets.token_urlsafe(32)}"),
        description="Master key for LiteLLM admin API (generated on first setup)",
    )
    database_url: str = Field(default="${GATEWAY_DB_URL}", description="Postgres connection URL")
    redis_url: str = Field(default="${GATEWAY_REDIS_URL}", description="Redis connection URL")
    background_health_checks: bool = True
    use_shared_health_check: bool = True


class MetaConfig(BaseModel):
    """Metadata about the config file itself."""
    schema_version: str = "1.0"


class GatewayConfig(BaseModel):
    """Top-level gateway configuration. This is the canonical schema."""

    model_config = {"protected_namespaces": ()}  # `model_list` is our field, not pydantic's

    meta: MetaConfig = Field(default_factory=MetaConfig)
    general_settings: GeneralSettings = Field(default_factory=GeneralSettings)
    routing_defaults: RoutingDefaults = Field(default_factory=RoutingDefaults)
    connectivity: ConnectivityConfig = Field(default_factory=ConnectivityConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    virtual_models: list[VirtualModelConfig] = Field(default_factory=list)
    litellm_settings: LiteLLMSettings = Field(default_factory=LiteLLMSettings)
    custom_providers: list[CustomProviderConfig] = Field(default_factory=list)
    # model_list is auto-generated by wizard + registry seeder.
    # Do not edit manually; use `gateway-cli models add/remove`.
    model_list: list[dict[str, Any]] = Field(default_factory=list, exclude=True)

    @field_validator("general_settings", mode="before")
    @classmethod
    def validate_master_key(cls, v: Any) -> Any:
        """Master key entropy check per Issue 12 (>= 32 chars)."""
        if isinstance(v, dict) and "master_key" in v:
            key = v["master_key"]
            if isinstance(key, str):
                # Unwrap a SecretStr-style repr defensively
                if len(key) < 32:
                    raise ValueError(
                        "master_key must be at least 32 characters for security"
                    )
        return v

    def to_yaml(self) -> str:
        """Serialize to YAML, excluding internal fields."""
        import yaml
        data = self.model_dump(exclude={"model_list"}, by_alias=True)
        # Handle SecretStr
        if "general_settings" in data and "master_key" in data["general_settings"]:
            data["general_settings"]["master_key"] = self.general_settings.master_key.get_secret_value()
        return yaml.dump(data, sort_keys=False, default_flow_style=False)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> "GatewayConfig":
        """Deserialize from YAML."""
        import yaml
        data = yaml.safe_load(yaml_str)
        return cls.model_validate(data)

    @classmethod
    def load_from_file(cls, path: str) -> "GatewayConfig":
        """Load and validate from a YAML file."""
        with open(path, "r") as f:
            return cls.from_yaml(f.read())

    def save_to_file(self, path: str) -> None:
        """Save to a YAML file."""
        with open(path, "w") as f:
            f.write(self.to_yaml())


# Default virtual models matching the plan
DEFAULT_VIRTUAL_MODELS = [
    VirtualModelConfig(
        name="auto-free",
        capabilities=["general"],
        tier="free",
        fallback_chain=[
            "nvidia-auto",
            "groq-auto-free",
            "cerebras-auto-free",
            "openrouter-free",
            "local-auto",
            "premium-auto",
        ],
    ),
    VirtualModelConfig(
        name="auto-code-free",
        capabilities=["code"],
        tier="free",
        fallback_chain=[
            "nvidia-code-free",
            "groq-code-free",
            "cerebras-code-free",
            "openrouter-code-free",
            "local-code",
            "premium-code",
        ],
    ),
    VirtualModelConfig(
        name="auto-reasoning-free",
        capabilities=["reasoning"],
        tier="free",
        fallback_chain=[
            "nvidia-reasoning-free",
            "groq-reasoning-free",
            "openrouter-reasoning-free",
            "local-reasoning",
            "premium-reasoning",
        ],
    ),
]


def create_default_config() -> GatewayConfig:
    """Create a default GatewayConfig with all defaults populated."""
    config = GatewayConfig()
    config.virtual_models = DEFAULT_VIRTUAL_MODELS
    return config