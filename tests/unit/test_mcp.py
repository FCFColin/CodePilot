"""MCP 客户端管理器单元测试。

覆盖：初始化、状态查询、工具名格式、配置集成。
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codepilot.config import Config
from codepilot.mcp.client import MCPClientManager


class TestMCPClientManagerInit:
    """MCPClientManager 初始化测试。"""

    def test_init_empty_configs(self) -> None:
        """空配置初始化。"""
        manager = MCPClientManager({})
        assert manager.server_configs == {}
        assert manager._connected is False
        assert manager.get_all_tools() == []

    def test_init_with_configs(self) -> None:
        """带配置初始化。"""
        configs = {
            "server1": {"command": "node", "args": ["server.js"]},
            "server2": {"url": "http://localhost:8080"},
        }
        manager = MCPClientManager(configs)
        assert len(manager.server_configs) == 2
        assert "server1" in manager.server_configs
        assert "server2" in manager.server_configs


class TestMCPClientManagerStatus:
    """MCPClientManager 状态查询测试。"""

    def test_status_not_connected(self) -> None:
        """未连接时的状态。"""
        configs = {"server1": {"command": "node"}}
        manager = MCPClientManager(configs)
        status = manager.get_status()
        assert status["connected"] is False
        assert "server1" in status["servers"]
        assert status["servers"]["server1"]["connected"] is False
        assert status["servers"]["server1"]["tools"] == 0

    def test_status_after_mock_connect(self) -> None:
        """模拟连接后的状态。"""
        configs = {"server1": {"command": "node"}}
        manager = MCPClientManager(configs)
        # 模拟连接成功
        manager._connected = True
        manager._sessions["server1"] = MagicMock()
        manager._tools["server1"] = [
            {"name": "server1__tool1", "description": "test tool"},
        ]
        status = manager.get_status()
        assert status["connected"] is True
        assert status["servers"]["server1"]["connected"] is True
        assert status["servers"]["server1"]["tools"] == 1


class TestMCPToolNameFormat:
    """MCP 工具名格式测试。"""

    @pytest.mark.asyncio
    async def test_call_tool_invalid_name(self) -> None:
        """无效的工具名格式。"""
        manager = MCPClientManager({})
        result = await manager.call_tool("invalid_name", {})
        assert result.startswith("Error: invalid MCP tool name format")

    @pytest.mark.asyncio
    async def test_call_tool_server_not_connected(self) -> None:
        """服务器未连接时调用工具。"""
        manager = MCPClientManager({"server1": {"command": "node"}})
        result = await manager.call_tool("server1__tool1", {})
        assert "not connected" in result

    @pytest.mark.asyncio
    async def test_call_tool_valid_format(self) -> None:
        """有效的工具名格式可正确拆分。"""
        manager = MCPClientManager({})
        # 模拟 session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text="result text")]
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        manager._sessions["server1"] = mock_session

        result = await manager.call_tool("server1__tool1", {"arg": "value"})
        assert result == "result text"
        mock_session.call_tool.assert_called_once_with("tool1", {"arg": "value"})

    @pytest.mark.asyncio
    async def test_call_tool_with_double_underscore_in_name(self) -> None:
        """工具名包含双下划线时正确拆分。"""
        manager = MCPClientManager({})
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = [MagicMock(text="ok")]
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        manager._sessions["srv"] = mock_session

        result = await manager.call_tool("srv__tool__sub", {})
        # split("__", 1) 只拆分第一个
        mock_session.call_tool.assert_called_once_with("tool__sub", {})


class TestMCPConfigInSettings:
    """MCP 配置在 Config 中的集成测试。"""

    def test_default_mcp_servers_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认 mcp_servers 为空字典。"""
        for key in list(os.environ.keys()):
            if key.startswith("CODEPILOT_"):
                monkeypatch.delenv(key, raising=False)
        config = Config()
        assert config.mcp_servers == {}

    def test_mcp_servers_with_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """通过构造函数设置 mcp_servers。"""
        for key in list(os.environ.keys()):
            if key.startswith("CODEPILOT_"):
                monkeypatch.delenv(key, raising=False)
        configs = {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem"],
            }
        }
        config = Config(mcp_servers=configs)
        assert "filesystem" in config.mcp_servers
        assert config.mcp_servers["filesystem"]["command"] == "npx"

    def test_get_all_tools_returns_flat_list(self) -> None:
        """get_all_tools 返回所有服务器的工具平铺列表。"""
        manager = MCPClientManager({})
        manager._tools = {
            "s1": [{"name": "s1__a"}, {"name": "s1__b"}],
            "s2": [{"name": "s2__c"}],
        }
        all_tools = manager.get_all_tools()
        assert len(all_tools) == 3
        assert all_tools[0]["name"] == "s1__a"
        assert all_tools[2]["name"] == "s2__c"
