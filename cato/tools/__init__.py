"""
cato/tools/__init__.py — Register all built-in tools with the agent loop.

Call register_all_tools(agent_loop) once at startup to wire every tool
handler into the loop's _TOOL_REGISTRY.
"""

from .browser import BrowserTool
from .file import FileTool
from .memory import MemoryTool
from .shell import ShellTool

__all__ = ["ShellTool", "FileTool", "BrowserTool", "MemoryTool"]


def register_all_tools(agent_loop) -> None:
    """Register all tools with the agent loop's tool registry."""
    agent_loop.register_tool("shell", ShellTool().execute)
    agent_loop.register_tool("file", FileTool().execute)
    agent_loop.register_tool("browser", BrowserTool().execute)
    agent_loop.register_tool("memory", MemoryTool().execute)
