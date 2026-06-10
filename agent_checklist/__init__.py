"""Agent middleware helpers.

Currently holds `context_trim_middleware.ContextTrimMiddleware`, which shrinks
old ToolMessage content before each LLM call once the in-flight messages exceed
a token threshold (see `context_trim_middleware.py`).
"""
