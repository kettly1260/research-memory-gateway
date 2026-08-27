"""Low-friction MCP surface intended for ordinary agents."""

from .capture import capture_memory
from .recall import get_project_state, recall_memory
from .tools import register_agent_tools
from .verify import verify_memory

__all__ = [
    "capture_memory",
    "get_project_state",
    "recall_memory",
    "register_agent_tools",
    "verify_memory",
]
