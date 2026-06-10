"""Map a chat to a `FlowSpec` (or None for no-op)."""

from __future__ import annotations

from .checker import FlowSpec
from .flows import ALL_FLOWS


def detect_flow_for_chat(project_id: str | None) -> FlowSpec | None:
    """Return the matching flow or `None` if this chat isn't in scope.

    Project-id based for the MVP — explicit, no guessing. Adding a new flow
    just means appending it to `verifier.flows.ALL_FLOWS` with its
    `project_ids` populated.
    """
    if not project_id:
        return None
    for flow in ALL_FLOWS:
        if project_id in flow.project_ids:
            return flow
    return None
