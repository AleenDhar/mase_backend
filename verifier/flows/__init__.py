"""Flow registry. Add a new flow by importing it and listing it in `ALL_FLOWS`."""

from .abm_v11 import ABM_V11_FLOW

ALL_FLOWS = (ABM_V11_FLOW,)

__all__ = ["ABM_V11_FLOW", "ALL_FLOWS"]
