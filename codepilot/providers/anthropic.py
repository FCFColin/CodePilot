"""Anthropic Provider 实现。

基于 anthropic Python SDK 原生 Messages API。
支持流式响应、工具调用、思考过程。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from codepilot.config import AnthropicConfig
from codepilot.providers.base import (
    AgentEvent,
    BaseProvider,
    Done,
    Message,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)


class AnthropicProvider(BaseProvider):
    """Anthropic Provider，基于 anthropic SDK 原生 Messages API。"""

    def __init__(self, config: AnthropicConfig) -> None:
        self.config = config
        # 创建异步 Anthropic 客户端
        self.client = anthropic.AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """发送消息并获取响应，返回 AgentEvent 事件流。"""
        # 构建请求消息列表（Anthropic 的 system 是顶层参数，不在 messages 中）
        request_messages = self._convert_messages(messages)

        # 构建请求参数
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": request_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

        # system_prompt 单独传递（Anthropic 顶层参数）
        if system_prompt:
            kwargs["system"] = system_prompt

        # 工具定义（Anthropic 原生格式：{"name": ..., "description": ..., "input_schema": {...}}）
        if tools:
            kwargs["tools"] = tools

        try:
            response = await self.client.messages.create(**kwargs)
        except anthropic.AnthropicError as e:
            # API 错误：发出 Done 事件含错误信息
            yield Done(stop_reason=f"error: {e}")
            return

        if stream:
            async for event in self._iter_stream(response):
                yield event
        else:
            async for event in self._iter_non_stream(response):
                yield event

    async def _iter_stream(self, response: Any) -> AsyncIterator[AgentEvent]:
        """解析流式响应事件为 AgentEvent。

        事件类型：
        - message_start → Usage（input_tokens）
        - content_block_start + text → 开始文本块
        - content_block_start + tool_use → 开始工具调用（记录 id, name）
        - content_block_delta + text_delta → TextDelta
        - content_block_delta + thinking_delta → ThinkingDelta
        - content_block_delta + input_json_delta → 累积工具参数 JSON
        - content_block_stop → 若是 tool_use 块，发出 ToolCall
        - message_delta → Usage（output_tokens）+ stop_reason
        - message_stop → Done
        """
        # 累积工具调用的中间状态：{block_index: {"id": ..., "name": ..., "arguments": ""}}
        pending_tool_calls: dict[int, dict[str, str]] = {}
        # 当前 content block 的类型和 index
        current_block_type: str | None = None
        current_block_index: int | None = None
        # stop_reason 在 message_delta 中传递，message_stop 时发出
        stop_reason = ""

        try:
            async for event in response:
                if event.type == "message_start":
                    # 初始 usage（input_tokens）
                    msg = event.message
                    if msg.usage:
                        yield Usage(
                            input_tokens=msg.usage.input_tokens,
                            output_tokens=msg.usage.output_tokens,
                        )

                elif event.type == "content_block_start":
                    block = event.content_block
                    current_block_type = block.type
                    current_block_index = event.index
                    # 工具调用开始：记录 id 和 name
                    if block.type == "tool_use":
                        pending_tool_calls[event.index] = {
                            "id": block.id,
                            "name": block.name,
                            "arguments": "",
                        }

                elif event.type == "content_block_delta":
                    delta = event.delta
                    # 文本片段
                    if delta.type == "text_delta":
                        yield TextDelta(text=delta.text)
                    # 思考过程片段
                    elif delta.type == "thinking_delta":
                        yield ThinkingDelta(text=delta.thinking)
                    # 工具调用参数 JSON 片段（累积）
                    elif delta.type == "input_json_delta":
                        if (
                            current_block_index is not None
                            and current_block_index in pending_tool_calls
                        ):
                            pending_tool_calls[current_block_index]["arguments"] += (
                                delta.partial_json
                            )

                elif event.type == "content_block_stop":
                    # 工具调用块结束：解析累积的 arguments 并发出 ToolCall
                    if (
                        current_block_type == "tool_use"
                        and current_block_index is not None
                    ):
                        tc_data = pending_tool_calls.get(current_block_index)
                        if tc_data:
                            try:
                                arguments = (
                                    json.loads(tc_data["arguments"])
                                    if tc_data["arguments"]
                                    else {}
                                )
                            except json.JSONDecodeError:
                                arguments = {}
                            yield ToolCall(
                                id=tc_data["id"],
                                name=tc_data["name"],
                                arguments=arguments,
                            )
                    current_block_type = None
                    current_block_index = None

                elif event.type == "message_delta":
                    # stop_reason 和 output_tokens
                    delta = event.delta
                    if delta and delta.stop_reason:
                        stop_reason = delta.stop_reason
                    if event.usage:
                        yield Usage(
                            input_tokens=0,
                            output_tokens=event.usage.output_tokens,
                        )

                elif event.type == "message_stop":
                    yield Done(stop_reason=stop_reason or "stop")

        except anthropic.AnthropicError as e:
            yield Done(stop_reason=f"error: {e}")

    async def _iter_non_stream(self, response: Any) -> AsyncIterator[AgentEvent]:
        """解析非流式响应为 AgentEvent。"""
        # usage
        if response.usage:
            yield Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        # content blocks
        for block in response.content:
            if block.type == "text":
                yield TextDelta(text=block.text)
            elif block.type == "thinking":
                yield ThinkingDelta(text=getattr(block, "thinking", ""))
            elif block.type == "tool_use":
                # input 已是 dict
                arguments = block.input if isinstance(block.input, dict) else {}
                yield ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=arguments,
                )

        yield Done(stop_reason=response.stop_reason or "stop")

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """将 Message 列表转换为 Anthropic 消息格式。

        - str content → {"role", "content"} 格式
        - list content → 透传（Anthropic content blocks）
        - 原始 dict → 透传（如 format_tool_result 的输出）
        - 嵌套 dict content（含 role 键）→ 提取内部 dict
          （format_assistant_message/format_tool_result 输出存为 content 时的还原）
        """
        result: list[dict] = []
        for msg in messages:
            if isinstance(msg, dict):
                # 已是 provider-native 格式（如工具结果），检查是否需要解嵌套
                content = msg.get("content")
                if isinstance(content, dict) and "role" in content:
                    # content 是完整的消息 dict（来自 format_assistant_message/format_tool_result）
                    result.append(content)
                else:
                    result.append(msg)
            elif isinstance(msg.content, dict) and "role" in msg.content:
                # content 是完整的消息 dict（来自 format_assistant_message/format_tool_result）
                result.append(msg.content)
            else:
                result.append({"role": msg.role, "content": msg.content})
        return result

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> dict:
        """将工具结果格式化为 Anthropic 的 tool_result 消息格式。

        Args:
            role: 消息角色（Anthropic 工具结果为 "user"）。
            tool_call_id: 对应的工具调用 ID（Anthropic 中为 tool_use_id）。
            content: 工具执行结果文本。

        Returns:
            Anthropic 格式的 tool_result 消息字典：
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}
        """
        return {
            "role": role,
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content,
                }
            ],
        }

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[ToolCall],
    ) -> dict:
        """将 assistant 文本与工具调用格式化为 Anthropic assistant 消息。

        Args:
            text: assistant 文本内容（可能为空字符串）。
            tool_calls: ToolCall 列表（可能为空）。

        Returns:
            Anthropic 格式的 assistant 消息字典：
            {"role": "assistant", "content": [
                {"type": "text", "text": text}?,  # text 非空时
                {"type": "tool_use", "id", "name", "input": dict}*
            ]}
        """
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            })
        return {"role": "assistant", "content": content}


__all__ = ["AnthropicProvider"]
