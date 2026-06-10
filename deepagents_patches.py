"""Runtime patches for the deepagents library.

`deepagents.create_deep_agent` hardcodes `TodoListMiddleware` (which registers
the built-in `write_todos` tool and injects its system-prompt instructions)
into the main agent and every subagent. There is no flag to disable it, so we
replace the class with a no-op middleware *before* any agent is built. This
removes `write_todos` from every tool schema and strips its prompt text, while
leaving all other deepagents behaviour untouched.

Patching happens against `deepagents.graph` (the module where create_deep_agent
looks the name up at call time). Idempotent and safe to call multiple times.
"""

from langchain.agents.middleware.types import AgentMiddleware


class _NoOpTodoListMiddleware(AgentMiddleware):
    """No-op stand-in for deepagents' built-in TodoListMiddleware.

    Registers no tools and injects no system prompt, so the agent never sees
    the `write_todos` tool.
    """


def disable_write_todos() -> None:
    """Replace deepagents' TodoListMiddleware with a no-op. Idempotent."""
    import deepagents.graph as _graph

    if getattr(_graph, "TodoListMiddleware", None) is not _NoOpTodoListMiddleware:
        _graph.TodoListMiddleware = _NoOpTodoListMiddleware
