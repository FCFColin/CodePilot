"""MCP 客户端管理器。

连接外部 MCP 服务器，将其工具暴露给 CodePilot agent。
支持 stdio 和 streamable_http 两种传输模式。
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import AsyncExitStack
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MCPClientManager:
    """管理多个 MCP 服务器连接。"""

    def __init__(self, server_configs: dict[str, dict]) -> None:
        self.server_configs = server_configs
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, list[dict]] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._connected = False

    async def connect_all(self) -> dict[str, list[dict]]:
        """连接所有配置的 MCP 服务器，返回所有可用工具。"""
        if self._connected:
            return self._tools

        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        for server_name, server_config in self.server_configs.items():
            try:
                tools = await self._connect_server(server_name, server_config)
                self._tools[server_name] = tools
                logger.info("MCP 服务器连接成功", server=server_name, tools=len(tools))
            except Exception as e:
                logger.warning(f"MCP 服务器连接失败: {server_name}", error=str(e))

        self._connected = True
        return self._tools

    async def _connect_server(self, name: str, config: dict) -> list[dict]:
        """连接单个 MCP 服务器。"""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            logger.error("mcp 包未安装，请运行: pip install mcp>=1.27,<2")
            raise

        if "command" in config:
            # stdio 传输
            env = dict(os.environ)
            env.update(config.get("env", {}))
            # 替换 ${VAR} 格式的环境变量
            for key, value in env.items():
                if isinstance(value, str) and "${" in value:
                    def replace_env(match: re.Match[str]) -> str:
                        var_name = match.group(1)
                        return os.environ.get(var_name, match.group(0))

                    env[key] = re.sub(r'\$\{(\w+)\}', replace_env, value)

            params = StdioServerParameters(
                command=config["command"],
                args=config.get("args", []),
                env=env,
            )
            stdio_transport = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(*stdio_transport)
            )
        elif "url" in config:
            # HTTP 传输
            try:
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError:
                logger.error("mcp HTTP 传输模式需要更新版本的 mcp 包")
                raise

            transport = await self._exit_stack.enter_async_context(
                streamablehttp_client(config["url"])
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(*transport)
            )
        else:
            raise ValueError(f"MCP 服务器配置缺少 command 或 url: {name}")

        await session.initialize()
        self._sessions[name] = session

        # 获取工具列表
        tools_result = await session.list_tools()
        return [
            {
                "server": name,
                "name": f"{name}__{tool.name}",
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "original_name": tool.name,
            }
            for tool in tools_result.tools
        ]

    async def call_tool(self, prefixed_name: str, arguments: dict) -> str:
        """调用 MCP 工具。"""
        if "__" not in prefixed_name:
            return f"Error: invalid MCP tool name format: {prefixed_name}"

        server_name, original_name = prefixed_name.split("__", 1)
        session = self._sessions.get(server_name)
        if session is None:
            return f"Error: MCP server '{server_name}' not connected"

        try:
            result = await session.call_tool(original_name, arguments)
            if result.content:
                return "\n".join(
                    item.text for item in result.content
                    if hasattr(item, 'text')
                )
            return "Tool executed successfully (no output)"
        except Exception as e:
            logger.error("MCP 工具调用失败", tool=prefixed_name, error=str(e))
            return f"Error calling MCP tool: {e}"

    def get_all_tools(self) -> list[dict]:
        """获取所有已连接服务器的工具列表。"""
        all_tools = []
        for server_tools in self._tools.values():
            all_tools.extend(server_tools)
        return all_tools

    def get_status(self) -> dict[str, Any]:
        """获取 MCP 连接状态。"""
        return {
            "connected": self._connected,
            "servers": {
                name: {
                    "connected": name in self._sessions,
                    "tools": len(self._tools.get(name, [])),
                }
                for name in self.server_configs
            },
        }

    async def disconnect_all(self) -> None:
        """断开所有 MCP 服务器连接。"""
        if self._exit_stack:
            await self._exit_stack.__aexit__(None, None, None)
            self._exit_stack = None
        self._sessions.clear()
        self._tools.clear()
        self._connected = False
        logger.info("所有 MCP 服务器已断开")
