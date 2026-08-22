"""
Extracted patterns for the LLM Gateway Phase 0 implementation.

This document contains the core implementation patterns discovered in the external repositories
that should be adopted for the LLM Gateway based on the review of all external repos.

Key patterns extracted:
1. Anthropic<->OpenAI translation logic (opencode-provider-nvidia-nim)
2. Health check probe design (free-router) 
3. Classifier dimensions (freerouter)
4. Config schema pattern (freerouter)
5. Quota tracking schema (llamux)
6. Rate limits data (llm-rate-limits-tracker)
"""

# ================================================
# 1. ANTHROPIC<->OPENAI TRANSLATION PATTERNS
# ================================================

"""
Source: opencode-provider-nvidia-nim/provider/translate.go (731 lines)

Core translation logic for Anthropic Messages API <-> OpenAI Chat Completions.

This is the primary reference implementation for the Anthropic Adapter in Phase 1.
"""

ANTHROPIC_TO_OPENAI_PATTERNS = {
    "system_prompt": {
        "source": "Top-level 'system' field in Anthropic request",
        "target": "First 'system' role message in OpenAI messages array",
        "example": {
            "anthropic": {"system": "You are a helpful assistant."},
            "openai": {"messages": [{"role": "system", "content": "You are a helpful assistant."}]}
        }
    },
    
    "tool_use": {
        "source": "Anthropic 'type': 'tool_use' content blocks",
        "target": "OpenAI 'tool_calls' array in message objects",
        "example": {
            "anthropic": {
                "content": [
                    {"type": "text", "text": "I'll help you with that."},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {"location": "NYC"}}
                ]
            },
            "openai": {
                "messages": [{
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "toolu_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": "{\"location\": \"NYC\"}"
                        }
                    }]
                }]
            }
        }
    },
    
    "tool_result": {
        "source": "Anthropic 'type': 'tool_result' content blocks",
        "target": "OpenAI 'tool_call_id' messages with 'role': 'tool'",
        "example": {
            "anthropic": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_123", "content": " sunny, 72°F"}
                ]
            },
            "openai": {
                "messages": [{
                    "role": "tool",
                    "tool_call_id": "toolu_123",
                    "content": " sunny, 72°F"
                }]
            }
        }
    },
    
    "image_content": {
        "source": "Anthropic 'type': 'image' with 'source.type': 'base64'",
        "target": "OpenAI 'type': 'image_url' with data URL",
        "example": {
            "anthropic": {
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUg..."}}
                ]
            },
            "openai": {
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."}
                    }]
                }]
            }
        }
    },
    
    "stop_reason": {
        "source": "Anthropic 'stop_reason' field (end_turn, tool_use, max_tokens)",
        "target": "OpenAI 'finish_reason' (stop, tool_calls, length)",
        "mapping": {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length"
        }
    },
    
    "streaming_events": {
        "source": "Anthropic SSE event types: content_block_start, content_block_delta, message_delta",
        "target": "OpenAI SSE event types: choices.delta, finish_reason",
        "event_mapping": {
            "content_block_start": "content_block_start",
            "content_block_delta": "content_block_delta", 
            "message_delta": "message_delta"
        }
    }
}

"""
HEALTH CHECK PROBE PATTERNS

Source: free-router (TUI-based health checking)

Core probe design patterns:
- Structured payload (not "ping")
- Extended timeout for cold starts
- Content-filter awareness
- Verdict classification
"""

HEALTH_PROBE_PATTERNS = {
    "structured_payload": {
        "value": {"messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_tokens": 3},
        "rationale": "Short, unambiguous prompt unlikely to trigger content filters"
    },
    
    "timeout_config": {
        "startup_timeout": 12000,  # milliseconds (12 seconds)
        "cold_start_accommodation": True,
        "provider_specific_overrides": True
    },
    
    "content_filter_awareness": {
        "400_with_content_filter": "Treat as healthy (model up, just refused)",
        "400_with_invalid_request": "Treat as misconfigured",
        "400_with_auth_error": "Unauthorized (needs human)"
    },
    
    "verdict_classification": {
        "200_ok_with_zero_usage": "Not Active (loaded but empty response)",
        "429_rate_limit": "Rate Limited (not unhealthy)",
        "503_server_error": "Unhealthy",
        "200_response_time_over_8s": "Slow (mark low-priority)"
    }
}

"""
CLASSIFIER DIMENSION PATTERNS  

Source: freerouter (14-dimension rule-based classifier)

Core 14-dimension scoring algorithm:
- Each dimension has a weight
- Rules cascade from SIMPLE to COMPLEX to REASONING to AGENTIC
- Overrides for large context, structured output, etc.
"""

CLASSIFIER_DIMENSIONS = [
    {
        "name": "message_count",
        "weight": 0.05,
        "description": "Number of messages in the conversation",
        "thresholds": {"simple": 5, "medium": 20, "complex": 50}
    },
    {
        "name": "input_token_count", 
        "weight": 0.10,
        "description": "Total tokens in user messages",
        "thresholds": {"simple": 200, "medium": 2000, "complex": 10000}
    },
    {
        "name": "output_token_estimate",
        "weight": 0.08,
        "description": "Estimated token output based on question complexity",
        "thresholds": {"simple": 100, "medium": 500, "complex": 2000}
    },
    {
        "name": "code_present",
        "weight": 0.12,
        "description": "Code blocks or programming language mentions",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "structured_output_required",
        "weight": 0.15,
        "description": "JSON, XML, or other structured format requests",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "tool_calls_required",
        "weight": 0.10,
        "description": "External tool/function calls requested",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "reasoning_complexity",
        "weight": 0.08,
        "description": "Logical reasoning depth and multi-step requirements",
        "thresholds": {"simple": 1, "medium": 2, "complex": 3}
    },
    {
        "name": "temporal_reasoning",
        "weight": 0.07,
        "description": "Time-based or sequential reasoning",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "causal_reasoning",
        "weight": 0.06,
        "description": "Cause-effect or correlation analysis",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "contextual_dependency",
        "weight": 0.04,
        "description": "Context-dependent or situational reasoning",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "creative_task",
        "weight": 0.03,
        "description": "Creative writing, brainstorming, or ideation",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "analytical_task",
        "weight": 0.02,
        "description": "Data analysis, research, or fact-checking",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    },
    {
        "name": "problem_solving",
        "weight": 0.05,
        "description": "Explicit problem-solving or troubleshooting",
        "thresholds": {"simple": 0, "medium": 1, "complex": 2}
    },
    {
        "name": "decision_making",
        "weight": 0.04,
        "description": "Making decisions based on multiple factors",
        "thresholds": {"simple": 0, "medium": 0, "complex": 1}
    }
]

TIER_ASSIGNMENT_RULES = [
    {
        "name": "SIMPLE",
        "description": "Basic Q&A, short responses, common knowledge",
        "max_score": 0.25,
        "model_types": ["general", "text"],
        "fallback_priorities": ["tier1_fallback", "tier2_fallback"]
    },
    {
        "name": "MEDIUM", 
        "description": "Intermediate complexity, moderate reasoning",
        "max_score": 0.45,
        "model_types": ["general", "code", "reasoning"],
        "fallback_priorities": ["tier2_fallback", "tier3_fallback"]
    },
    {
        "name": "COMPLEX",
        "description": "High complexity, advanced reasoning, multi-step",
        "max_score": 0.65,
        "model_types": ["code", "reasoning", "agentic"],
        "fallback_priorities": ["tier3_fallback", "tier4_fallback"]
    },
    {
        "name": "REASONING",
        "description": "Advanced logical reasoning, multi-step chains",
        "max_score": 0.75,
        "model_types": ["reasoning", "agentic"],
        "fallback_priorities": ["tier3_fallback", "tier4_fallback"]
    },
    {
        "name": "AGENTIC",
        "description": "High-level agentic tasks, orchestration",
        "max_score": 0.85,
        "model_types": ["agentic"],
        "fallback_priorities": ["tier4_fallback", "tier5_fallback"]
    }
]

"""
CONFIG SCHEMA PATTERN

Source: freerouter/src/config.ts

Deep merge of multiple sources with validation.
"""

CONFIG_PATTERN = {
    "sources_order": [
        "FREEROUTER_CONFIG env var",
        "./freerouter.config.json (cwd)", 
        "~/.config/freerouter/config.json (user)",
        "Default builtin values"
    ],
    "merge_strategy": "Deep merge with env vars taking precedence",
    "validation": "Schema validation with error messages",
    "defaults": {
        "routing": {
            "max_concurrent": 10,
            "retry_attempts": 3,
            "timeout_ms": 30000,
            "health_check_interval": 60000
        },
        "models": {
            "max_context_length": 131072,
            "default_temperature": 0.7,
            "default_top_p": 0.9
        }
    }
}

"""
QUOTA TRACKING PATTERN

Source: llamux-llm-router (CSV-driven quota system)
"""

QUOTA_COLUMNS = [
    "provider", "model", "rpm", "tpm", "rph", "tph", "rpd", "tpd",
    "requests_count", "tokens_count", "requests_used", "tokens_used",
    "last_reset", "status"
]

QUOTA_BEHAVIOR = {
    "exceeded_action": "Fall back to next provider in chain",
    "reset_strategy": "Sliding window per period (RPM, TPM, etc.)",
    "monitoring": "Track quota usage in real-time",
    "fallback_logic": "Preferences follow CSV row order (first = highest priority)"
}

"""
RATE LIMITS DATA PATTERN

Source: llm-rate-limits-tracker/data/rate-limits.json

Weekly-updated external data source with provider/model tier limits.
"""

RATE_LIMITS_STRUCTURE = {
    "primary_keys": ["provider_id", "model_id"],
    "tiers": ["free", "tier-1", "tier-2", "tier-3", "tier-4", "tier-5"],
    "fields": ["spend_threshold_usd", "rpm", "tpm", "rph", "tph", "rpd", "tpd", "notes"],
    "source": "CDN-hosted JSON at https://cdn.jsdelivr.net/gh/llerandi/llm-rate-limits-tracker@main/data/rate-limits.json"
}

"""
IMPLEMENTATION RECOMMENDATIONS

Based on all extracted patterns:

1. Use opencode-provider-nvidia-nim translate.go as primary reference for Anthropic Adapter
2. Adopt free-router health probe design for structured probing
3. Implement freerouter 14-dimension classifier for routing decisions  
4. Use freerouter config pattern for GatewayConfig
5. Adopt llamux CSV quota tracking for model_registry quotas
6. Integrate llm-rate-limits-tracker for dynamic quota seeding
7. Maintain two-process architecture (Gateway + Brain) as per plan
"""

IMPLEMENTATION_MAP = {
    "anthropic_adapter": {
        "reference_repo": "opencode-provider-nvidia-nim",
        "key_file": "translate.go",
        "lines": 731,
        "language": "Go"
    },
    "health_check": {
        "reference_repo": "free-router", 
        "key_file": "ui/main.go",
        "language": "Go",
        "pattern": "structured_payload + 12s_timeout + content_filter_awareness"
    },
    "routing_brain": {
        "reference_repo": "freerouter",
        "key_file": "src/router/index.ts", 
        "lines": 731,
        "language": "TypeScript",
        "pattern": "14_dimensions + tiered_fallbacks"
    },
    "config_schema": {
        "reference_repo": "freerouter",
        "key_file": "src/config.ts",
        "language": "TypeScript", 
        "pattern": "env+file+defaults deep merge"
    },
    "quota_tracking": {
        "reference_repo": "llamux-llm-router",
        "key_file": "main.go",
        "language": "Go",
        "pattern": "CSV columns + sliding window"
    },
    "rate_limits_data": {
        "reference_repo": "llm-rate-limits-tracker",
        "key_file": "data/rate-limits.json",
        "lines": 1117,
        "update_frequency": "weekly"
    }
}