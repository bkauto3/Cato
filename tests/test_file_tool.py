"""File tool security tests."""
import json
import pytest
import asyncio
import cato.tools.file as ft
from cato.tools.file import FileTool


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path):
    tool = FileTool()
    # Attempt path traversal
    raw = await tool.execute({"action": "read", "path": "../../etc/passwd", "agent_id": "test"})
    result = json.loads(raw)
    assert result.get("success") is False or "error" in result


@pytest.mark.asyncio
async def test_valid_read_write(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "_WORKSPACE_ROOT", tmp_path)
    tool = FileTool()
    # Write then read
    write_raw = await tool.execute({"action": "write", "path": "test.txt", "content": "hello", "agent_id": "test"})
    write_result = json.loads(write_raw)
    assert write_result.get("success") is True
    read_raw = await tool.execute({"action": "read", "path": "test.txt", "agent_id": "test"})
    read_result = json.loads(read_raw)
    assert "hello" in read_result.get("content", "")
